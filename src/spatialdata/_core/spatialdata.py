from __future__ import annotations

import hashlib
import os
import warnings
from collections.abc import Generator
from itertools import chain
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import pandas as pd
import zarr
from anndata import AnnData
from dask.dataframe import read_parquet
from dask.dataframe.core import DataFrame as DaskDataFrame
from dask.delayed import Delayed
from geopandas import GeoDataFrame
from multiscale_spatial_image.multiscale_spatial_image import MultiscaleSpatialImage
from ome_zarr.io import parse_url
from ome_zarr.types import JSONDict
from shapely import MultiPolygon, Polygon
from spatial_image import SpatialImage

from spatialdata._core._elements import Images, Labels, Points, Shapes, Tables
from spatialdata._logging import logger
from spatialdata._types import ArrayLike, Raster_T
from spatialdata._utils import _error_message_add_element, deprecation_alias
from spatialdata.models import (
    Image2DModel,
    Image3DModel,
    Labels2DModel,
    Labels3DModel,
    PointsModel,
    ShapesModel,
    TableModel,
    check_target_region_column_symmetry,
    get_model,
    get_table_keys,
)
from spatialdata.models._utils import SpatialElement, get_axes_names

if TYPE_CHECKING:
    from spatialdata._core.query.spatial_query import BaseSpatialRequest

# schema for elements
Label2D_s = Labels2DModel()
Label3D_s = Labels3DModel()
Image2D_s = Image2DModel()
Image3D_s = Image3DModel()
Shape_s = ShapesModel()
Point_s = PointsModel()
Table_s = TableModel()


class SpatialData:
    """
    The SpatialData object.

    The SpatialData object is a modular container for arbitrary combinations of SpatialElements. The elements
    can be accesses separately and are stored as standard types (:class:`anndata.AnnData`,
    :class:`geopandas.GeoDataFrame`, :class:`xarray.DataArray`).


    Parameters
    ----------
    images
        Dict of 2D and 3D image elements. The following parsers are available: :class:`~spatialdata.Image2DModel`,
        :class:`~spatialdata.Image3DModel`.
    labels
        Dict of 2D and 3D labels elements. Labels are regions, they can't contain annotation, but they can be
        annotated by a table. The following parsers are available: :class:`~spatialdata.Labels2DModel`,
        :class:`~spatialdata.Labels3DModel`.
    points
        Dict of points elements. Points can contain annotations. The following parsers is available:
        :class:`~spatialdata.PointsModel`.
    shapes
        Dict of 2D shapes elements (circles, polygons, multipolygons).
        Shapes are regions, they can't contain annotation, but they can be annotated by a table.
        The following parsers are available: :class:`~spatialdata.ShapesModel`.
    table
        AnnData table containing annotations for regions (labels and shapes). The following parsers is
        available: :class:`~spatialdata.TableModel`.

    Notes
    -----
    The SpatialElements are stored with standard types:

        - images and labels are stored as :class:`spatial_image.SpatialImage` or
            :class:`multiscale_spatial_image.MultiscaleSpatialImage` objects, which are respectively equivalent to
            :class:`xarray.DataArray` and to a :class:`datatree.DataTree` of :class:`xarray.DataArray` objects.
        - points are stored as :class:`dask.dataframe.DataFrame` objects.
        - shapes are stored as :class:`geopandas.GeoDataFrame`.
        - the table are stored as :class:`anndata.AnnData` objects,  with the spatial coordinates stored in the obsm
            slot.

    The table can annotate regions (shapesor labels) and can be used to store additional information.
    Points are not regions but 0-dimensional locations. They can't be annotated by a table, but they can store
    annotation directly.

    The elements need to pass a validation step. To construct valid elements you can use the parsers that we
    provide:

        - :class:`~spatialdata.Image2DModel`,
        - :class:`~spatialdata.Image3DModel`,
        - :class:`~spatialdata.Labels2DModel`,
        - :class:`~spatialdata.Labels3DModel`,
        - :class:`~spatialdata.PointsModel`,
        - :class:`~spatialdata.ShapesModel`,
        - :class:`~spatialdata.TableModel`

    """

    @deprecation_alias(table="tables")
    def __init__(
        self,
        images: dict[str, Raster_T] | None = None,
        labels: dict[str, Raster_T] | None = None,
        points: dict[str, DaskDataFrame] | None = None,
        shapes: dict[str, GeoDataFrame] | None = None,
        tables: dict[str, AnnData] | Tables | None = None,
    ) -> None:
        self._path: Path | None = None

        self._shared_keys: set[str | None] = set()
        self._images: Images = Images(shared_keys=self._shared_keys)
        self._labels: Labels = Labels(shared_keys=self._shared_keys)
        self._points: Points = Points(shared_keys=self._shared_keys)
        self._shapes: Shapes = Shapes(shared_keys=self._shared_keys)
        self._tables: Tables = Tables(shared_keys=self._shared_keys)

        # Workaround to allow for backward compatibility
        if isinstance(tables, AnnData):
            tables = {"table": tables}

        element_names = list(chain.from_iterable([e.keys() for e in [images, labels, points, shapes] if e is not None]))

        if len(element_names) != len(set(element_names)):
            duplicates = {x for x in element_names if element_names.count(x) > 1}
            raise KeyError(
                f"Element names must be unique. The following element names are used multiple times: {duplicates}"
            )

        if images is not None:
            for k, v in images.items():
                self.images[k] = v

        if labels is not None:
            for k, v in labels.items():
                self.labels[k] = v

        if shapes is not None:
            for k, v in shapes.items():
                self.shapes[k] = v

        if points is not None:
            for k, v in points.items():
                self.points[k] = v

        if tables is not None:
            for k, v in tables.items():
                self.validate_table_in_spatialdata(v)
                self.tables[k] = v

        self._query = QueryManager(self)

    def validate_table_in_spatialdata(self, table: AnnData) -> None:
        """
        Validate the presence of the annotation target of a SpatialData table in the SpatialData object.

        This method validates a table in the SpatialData object to ensure that if annotation metadata is present, the
        annotation target (SpatialElement) is present in the SpatialData object, the dtypes of the instance key column
        in the table and the annotation target do not match. Otherwise, a warning is raised.

        Parameters
        ----------
        table
            The table potentially annotating a SpatialElement

        Raises
        ------
        UserWarning
            If the table is annotating elements not present in the SpatialData object.
        UserWarning
            The dtypes of the instance key column in the table and the annotation target do not match.
        """
        TableModel().validate(table)
        if TableModel.ATTRS_KEY in table.uns:
            region, _, instance_key = get_table_keys(table)
            region = region if isinstance(region, list) else [region]
            for r in region:
                element = self.get(r)
                if element is None:
                    warnings.warn(
                        f"The table is annotating {r!r}, which is not present in the SpatialData object.",
                        UserWarning,
                        stacklevel=2,
                    )
                else:
                    if isinstance(element, SpatialImage):
                        dtype = element.dtype
                    elif isinstance(element, MultiscaleSpatialImage):
                        dtype = element.scale0.ds.dtypes["image"]
                    else:
                        dtype = element.index.dtype
                    if dtype != table.obs[instance_key].dtype and (
                        dtype == str or table.obs[instance_key].dtype == str
                    ):
                        raise TypeError(
                            f"Table instance_key column ({instance_key}) has a dtype "
                            f"({table.obs[instance_key].dtype}) that does not match the dtype of the indices of "
                            f"the annotated element ({dtype})."
                        )

    @staticmethod
    def from_elements_dict(elements_dict: dict[str, SpatialElement | AnnData]) -> SpatialData:
        """
        Create a SpatialData object from a dict of elements.

        Parameters
        ----------
        elements_dict
            Dict of elements. The keys are the names of the elements and the values are the elements.
            A table can be present in the dict, but only at most one; its name is not used and can be anything.

        Returns
        -------
        The SpatialData object.
        """
        d: dict[str, dict[str, SpatialElement] | AnnData | None] = {
            "images": {},
            "labels": {},
            "points": {},
            "shapes": {},
            "tables": {},
        }
        for k, e in elements_dict.items():
            schema = get_model(e)
            if schema in (Image2DModel, Image3DModel):
                assert isinstance(d["images"], dict)
                d["images"][k] = e
            elif schema in (Labels2DModel, Labels3DModel):
                assert isinstance(d["labels"], dict)
                d["labels"][k] = e
            elif schema == PointsModel:
                assert isinstance(d["points"], dict)
                d["points"][k] = e
            elif schema == ShapesModel:
                assert isinstance(d["shapes"], dict)
                d["shapes"][k] = e
            elif schema == TableModel:
                assert isinstance(d["tables"], dict)
                d["tables"][k] = e
            else:
                raise ValueError(f"Unknown schema {schema}")
        return SpatialData(**d)  # type: ignore[arg-type]

    @staticmethod
    def get_annotated_regions(table: AnnData) -> str | list[str]:
        """
        Get the regions annotated by a table.

        Parameters
        ----------
        table
            The AnnData table for which to retrieve annotated regions.

        Returns
        -------
        The annotated regions.
        """
        regions, _, _ = get_table_keys(table)
        return regions

    @staticmethod
    def get_region_key_column(table: AnnData) -> pd.Series:
        """Get the column of table.obs containing per row the region annotated by that row.

        Parameters
        ----------
        table
            The AnnData table.

        Returns
        -------
        The region key column.

        Raises
        ------
        KeyError
            If the region key column is not found in table.obs.
        """
        _, region_key, _ = get_table_keys(table)
        if table.obs.get(region_key):
            return table.obs[region_key]
        raise KeyError(f"{region_key} is set as region key column. However the column is not found in table.obs.")

    @staticmethod
    def get_instance_key_column(table: AnnData) -> pd.Series:
        """
        Return the instance key column in table.obs containing for each row the instance id of that row.

        Parameters
        ----------
        table
            The AnnData table.

        Returns
        -------
        The instance key column.

        Raises
        ------
        KeyError
            If the instance key column is not found in table.obs.

        """
        _, _, instance_key = get_table_keys(table)
        if table.obs.get(instance_key):
            return table.obs[instance_key]
        raise KeyError(f"{instance_key} is set as instance key column. However the column is not found in table.obs.")

    @staticmethod
    def _set_table_annotation_target(
        table: AnnData,
        region: str | pd.Series,
        region_key: str,
        instance_key: str,
    ) -> None:
        """
        Set the SpatialElement annotation target of an AnnData table.

        This method sets the target annotation element of a table  based on the specified parameters. It creates the
        `attrs` dictionary for `table.uns` and only after validation that the regions are present in the region_key
        column of table.obs updates the annotation metadata of the table.

        Parameters
        ----------
        table
            The AnnData object containing the data table.
        region
            The name of the target element for the table annotation.
        region_key
            The key for the region annotation column in `table.obs`.
        instance_key
            The key for the instance annotation column in `table.obs`.

        Raises
        ------
        ValueError
            If `region_key` is not present in the `table.obs` columns.
        ValueError
            If `instance_key` is not present in the `table.obs` columns.
        """
        TableModel()._validate_set_region_key(table, region_key)
        TableModel()._validate_set_instance_key(table, instance_key)
        attrs = {
            TableModel.REGION_KEY: region,
            TableModel.REGION_KEY_KEY: region_key,
            TableModel.INSTANCE_KEY: instance_key,
        }
        check_target_region_column_symmetry(table, region_key, region)
        table.uns[TableModel.ATTRS_KEY] = attrs

    @staticmethod
    def _change_table_annotation_target(
        table: AnnData,
        region: str | pd.Series,
        region_key: None | str = None,
        instance_key: None | str = None,
    ) -> None:
        """Change the annotation target of a table currently having annotation metadata already.

        Parameters
        ----------
        table
            The table already annotating a SpatialElement.
        region
            The name of the target SpatialElement for which the table annotation will be changed.
        region_key
            The name of the region key column in the table. If not provided, it will be extracted from the table's uns
            attribute. If present here but also given as argument, the value in the table's uns attribute will be
            overwritten.
        instance_key
            The name of the instance key column in the table. If not provided, it will be extracted from the table's uns
            attribute. If present here but also given as argument, the value in the table's uns attribute will be
            overwritten.

        Raises
        ------
        ValueError
            If no region_key is provided, and it is not present in both table.uns['spatialdata_attrs'] and table.obs.
        ValueError
            If provided region_key is not present in table.obs.
        """
        attrs = table.uns[TableModel.ATTRS_KEY]
        table_region_key = region_key if region_key else attrs.get(TableModel.REGION_KEY_KEY)

        TableModel()._validate_set_region_key(table, region_key)
        TableModel()._validate_set_instance_key(table, instance_key)
        check_target_region_column_symmetry(table, table_region_key, region)
        attrs[TableModel.REGION_KEY] = region

    def set_table_annotates_spatialelement(
        self,
        table_name: str,
        region: str | pd.Series,
        region_key: None | str = None,
        instance_key: None | str = None,
    ) -> None:
        """
        Set the SpatialElement annotation target of a given AnnData table.

        Parameters
        ----------
        table_name
            The name of the table to set the annotation target for.
        region
            The name of the target element for the annotation. This can either be a string or a pandas Series object.
        region_key
            The region key for the annotation. If not specified, defaults to None which means the currently set region
            key is reused.
        instance_key
            The instance key for the annotation. If not specified, defaults to None which means the currently set
            instance key is reused.

        Raises
        ------
        ValueError
            If the annotation SpatialElement target is not present in the SpatialData object.
        TypeError
            If no current annotation metadata is found and both region_key and instance_key are not specified.
        """
        table = self.tables[table_name]
        element_names = {element[1] for element in self._gen_elements()}
        if region not in element_names:
            raise ValueError(f"Annotation target '{region}' not present as SpatialElement in SpatialData object.")

        if table.uns.get(TableModel.ATTRS_KEY):
            self._change_table_annotation_target(table, region, region_key, instance_key)
        elif isinstance(region_key, str) and isinstance(instance_key, str):
            self._set_table_annotation_target(table, region, region_key, instance_key)
        else:
            raise TypeError("No current annotation metadata found. Please specify both region_key and instance_key.")

    @property
    def query(self) -> QueryManager:
        return self._query

    def aggregate(
        self,
        values_sdata: SpatialData | None = None,
        values: DaskDataFrame | GeoDataFrame | SpatialImage | MultiscaleSpatialImage | str | None = None,
        by_sdata: SpatialData | None = None,
        by: GeoDataFrame | SpatialImage | MultiscaleSpatialImage | str | None = None,
        value_key: list[str] | str | None = None,
        agg_func: str | list[str] = "sum",
        target_coordinate_system: str = "global",
        fractions: bool = False,
        region_key: str = "region",
        instance_key: str = "instance_id",
        deepcopy: bool = True,
        table_name: str = "table",
        **kwargs: Any,
    ) -> SpatialData:
        """
        Aggregate values by given region.

        Notes
        -----
        This function calls :func:`spatialdata.aggregate` with the convenience that `values` and `by` can be string
        without having to specify the `values_sdata` and `by_sdata`, which in that case will be replaced by `self`.

        Please see
        :func:`spatialdata.aggregate` for the complete docstring.
        """
        from spatialdata._core.operations.aggregate import aggregate

        if isinstance(values, str) and values_sdata is None:
            values_sdata = self
        if isinstance(by, str) and by_sdata is None:
            by_sdata = self

        return aggregate(
            values_sdata=values_sdata,
            values=values,
            by_sdata=by_sdata,
            by=by,
            value_key=value_key,
            agg_func=agg_func,
            target_coordinate_system=target_coordinate_system,
            fractions=fractions,
            region_key=region_key,
            instance_key=instance_key,
            deepcopy=deepcopy,
            table_name=table_name,
            **kwargs,
        )

    def is_backed(self) -> bool:
        """Check if the data is backed by a Zarr storage or if it is in-memory."""
        return self.path is not None

    @property
    def path(self) -> Path | None:
        """Path to the Zarr storage."""
        if self._path is None:
            return None
        if isinstance(self._path, str):
            return Path(self._path)
        if isinstance(self._path, Path):
            return self._path
        raise ValueError(f"Unexpected type for path: {type(self._path)}")

    @path.setter
    def path(self, value: Path | None) -> None:
        self._path = value
        if not self.is_self_contained():
            logger.info(
                "The SpatialData object is not self-contained (i.e. it contains some elements that are Dask-backed from"
                f" locations outside {self._path}). Please see the documentation of `is_self_contained()` to understand"
                f" the implications of working with SpatialData objects that are not self-contained."
            )

    def _get_groups_for_element(
        self, zarr_path: Path, element_type: str, element_name: str
    ) -> tuple[zarr.Group, zarr.Group, zarr.Group]:
        """
        Get the Zarr groups for the root, element_type and element for a specific element.

        The store must exist, but creates the element type group and the element group if they don't exist.

        Parameters
        ----------
        zarr_path
            The path to the Zarr storage.
        element_type
            type of the element; must be in ["images", "labels", "points", "polygons", "shapes", "tables"].
        element_name
            name of the element

        Returns
        -------
        either the existing Zarr subgroup or a new one.
        """
        if not isinstance(zarr_path, Path):
            raise ValueError("zarr_path should be a Path object")
        store = parse_url(zarr_path, mode="r+").store
        root = zarr.group(store=store)
        if element_type not in ["images", "labels", "points", "polygons", "shapes", "tables"]:
            raise ValueError(f"Unknown element type {element_type}")
        element_type_group = root.require_group(element_type)
        element_name_group = element_type_group.require_group(element_name)
        return root, element_type_group, element_name_group

    def _group_for_element_exists(self, zarr_path: Path, element_type: str, element_name: str) -> bool:
        """
        Check if the group for an element exists.

        Parameters
        ----------
        element_type
            type of the element; must be in ["images", "labels", "points", "polygons", "shapes", "tables"].
        element_name
            name of the element

        Returns
        -------
        True if the group exists, False otherwise.
        """
        store = parse_url(zarr_path, mode="r").store
        root = zarr.group(store=store)
        assert element_type in ["images", "labels", "points", "polygons", "shapes", "tables"]
        exists = element_type in root and element_name in root[element_type]
        store.close()
        return exists

    def locate_element(self, element: SpatialElement) -> list[str]:
        """
        Locate a SpatialElement within the SpatialData object and returns its Zarr paths relative to the root.

        Parameters
        ----------
        element
            The queried SpatialElement

        Returns
        -------
        A list of Zarr paths of the element relative to the root (multiple copies of the same element are allowed).
        The list is empty if the element is not present.
        """
        found: list[SpatialElement] = []
        found_element_type: list[str] = []
        found_element_name: list[str] = []
        for element_type in ["images", "labels", "points", "shapes"]:
            for element_name, element_value in getattr(self, element_type).items():
                if id(element_value) == id(element):
                    found.append(element_value)
                    found_element_type.append(element_type)
                    found_element_name.append(element_name)
        if len(found) == 0:
            return []
        if any("/" in found_element_name[i] or "/" in found_element_type[i] for i in range(len(found))):
            raise ValueError("Found an element name with a '/' character. This is not allowed.")
        return [f"{found_element_type[i]}/{found_element_name[i]}" for i in range(len(found))]

    @deprecation_alias(filter_table="filter_tables")
    def filter_by_coordinate_system(
        self, coordinate_system: str | list[str], filter_tables: bool = True, include_orphan_tables: bool = False
    ) -> SpatialData:
        """
        Filter the SpatialData by one (or a list of) coordinate system.

        This returns a SpatialData object with the elements containing a transformation mapping to the specified
        coordinate system(s).

        Parameters
        ----------
        coordinate_system
            The coordinate system(s) to filter by.
        filter_tables
            If True (default), the tables will be filtered to only contain regions
            of an element belonging to the specified coordinate system(s).
        include_orphan_tables
            If True (not default), include tables that do not annotate SpatialElement(s). Only has an effect if
            filter_tables is also set to True.

        Returns
        -------
        The filtered SpatialData.
        """
        # TODO: decide whether to add parameter to filter only specific table.

        from spatialdata.transformations.operations import get_transformation

        elements: dict[str, dict[str, SpatialElement]] = {}
        element_names_in_coordinate_system = []
        if isinstance(coordinate_system, str):
            coordinate_system = [coordinate_system]
        for element_type, element_name, element in self._gen_elements():
            if element_type != "tables":
                transformations = get_transformation(element, get_all=True)
                assert isinstance(transformations, dict)
                for cs in coordinate_system:
                    if cs in transformations:
                        if element_type not in elements:
                            elements[element_type] = {}
                        elements[element_type][element_name] = element
                        element_names_in_coordinate_system.append(element_name)
        tables = self._filter_tables(
            set(), filter_tables, "cs", include_orphan_tables, element_names=element_names_in_coordinate_system
        )

        return SpatialData(**elements, tables=tables)

    # TODO: move to relational query with refactor
    def _filter_tables(
        self,
        names_tables_to_keep: set[str],
        filter_tables: bool = True,
        by: Literal["cs", "elements"] | None = None,
        include_orphan_tables: bool = False,
        element_names: str | list[str] | None = None,
        elements_dict: dict[str, dict[str, Any]] | None = None,
    ) -> Tables | dict[str, AnnData]:
        """
        Filter tables by coordinate system or elements or return tables.

        Parameters
        ----------
        names_tables_to_keep
            The names of the tables to keep even when filter_tables is True.
        filter_tables
            If True (default), the tables will be filtered to only contain regions
            of an element belonging to the specified coordinate system(s) or including only rows annotating specified
            elements.
        by
            Filter mode. Valid values are "cs" or "elements". Default is None.
        include_orphan_tables
            Flag indicating whether to include orphan tables. Default is False.
        element_names
            Element names of elements present in specific coordinate system.
        elements_dict
            Dictionary of elements for filtering the tables. Default is None.

        Returns
        -------
        The filtered tables if filter_tables was True, otherwise tables of the SpatialData object.

        """
        if filter_tables:
            tables: dict[str, AnnData] | Tables = {}
            for table_name, table in self._tables.items():
                if include_orphan_tables and not table.uns.get(TableModel.ATTRS_KEY):
                    tables[table_name] = table
                    continue
                if table_name in names_tables_to_keep:
                    tables[table_name] = table
                    continue
                # each mode here requires paths or elements, using assert here to avoid mypy errors.
                if by == "cs":
                    from spatialdata._core.query.relational_query import _filter_table_by_element_names

                    assert element_names is not None
                    table = _filter_table_by_element_names(table, element_names)
                    if len(table) != 0:
                        tables[table_name] = table
                elif by == "elements":
                    from spatialdata._core.query.relational_query import _filter_table_by_elements

                    assert elements_dict is not None
                    table = _filter_table_by_elements(table, elements_dict=elements_dict)
                    if len(table) != 0:
                        tables[table_name] = table
        else:
            tables = self.tables

        return tables

    def rename_coordinate_systems(self, rename_dict: dict[str, str]) -> None:
        """
        Rename coordinate systems.

        Parameters
        ----------
        rename_dict
            A dictionary mapping old coordinate system names to new coordinate system names.

        Notes
        -----
        The method does not allow to rename a coordinate system into an existing one, unless the existing one is also
        renamed in the same call.
        """
        from spatialdata.transformations.operations import get_transformation, set_transformation

        # check that the rename_dict is valid
        old_names = self.coordinate_systems
        new_names = list(set(old_names).difference(set(rename_dict.keys())))
        for old_cs, new_cs in rename_dict.items():
            if old_cs not in old_names:
                raise ValueError(f"Coordinate system {old_cs} does not exist.")
            if new_cs in new_names:
                raise ValueError(
                    "It is not allowed to rename a coordinate system if the new name already exists and "
                    "if it is not renamed in the same call."
                )
            new_names.append(new_cs)

        # rename the coordinate systems
        for element in self._gen_spatial_element_values():
            # get the transformations
            transformations = get_transformation(element, get_all=True)
            assert isinstance(transformations, dict)

            # appends a random suffix to the coordinate system name to avoid collisions
            suffixes_to_replace = set()
            for old_cs, new_cs in rename_dict.items():
                if old_cs in transformations:
                    random_suffix = hashlib.sha1(os.urandom(128)).hexdigest()[:8]
                    transformations[new_cs + random_suffix] = transformations.pop(old_cs)
                    suffixes_to_replace.add(new_cs + random_suffix)

            # remove the random suffixes
            new_transformations = {}
            for cs_with_suffix in transformations:
                if cs_with_suffix in suffixes_to_replace:
                    cs = cs_with_suffix[:-8]
                    new_transformations[cs] = transformations[cs_with_suffix]
                    suffixes_to_replace.remove(cs_with_suffix)
                else:
                    new_transformations[cs_with_suffix] = transformations[cs_with_suffix]

            # set the new transformations
            set_transformation(element=element, transformation=new_transformations, set_all=True)

    def transform_element_to_coordinate_system(
        self, element: SpatialElement, target_coordinate_system: str, maintain_positioning: bool = False
    ) -> SpatialElement:
        """
        Transform an element to a given coordinate system.

        Parameters
        ----------
        element
            The element to transform.
        target_coordinate_system
            The target coordinate system.
        maintain_positioning
            Default False (most common use case). If True, the data will be transformed but a transformation will be
            added so that the positioning of the data in the target coordinate system will not change. If you want to
            align datasets to a common coordinate system you should use the default value.

        Returns
        -------
        The transformed element.
        """
        from spatialdata import transform
        from spatialdata.transformations import Sequence
        from spatialdata.transformations.operations import (
            get_transformation,
            get_transformation_between_coordinate_systems,
            remove_transformation,
            set_transformation,
        )

        t = get_transformation_between_coordinate_systems(self, element, target_coordinate_system)
        if maintain_positioning:
            transformed = transform(element, transformation=t, maintain_positioning=maintain_positioning)
        else:
            d = get_transformation(element, get_all=True)
            assert isinstance(d, dict)
            to_remove = False
            if target_coordinate_system not in d:
                d[target_coordinate_system] = t
                to_remove = True
            transformed = transform(
                element, to_coordinate_system=target_coordinate_system, maintain_positioning=maintain_positioning
            )
            if to_remove:
                del d[target_coordinate_system]
        if not maintain_positioning:
            d = get_transformation(transformed, get_all=True)
            assert isinstance(d, dict)
            assert len(d) == 1
            t = list(d.values())[0]
            remove_transformation(transformed, remove_all=True)
            set_transformation(transformed, t, target_coordinate_system)
        else:
            # When maintaining positioning is true, and if the element has a transformation to target_coordinate_system
            # (this may not be the case because it could be that the element is not directly mapped to that coordinate
            # system), then the transformation to the target coordinate system is not needed # because the data is now
            # already transformed; here we remove such transformation.
            d = get_transformation(transformed, get_all=True)
            assert isinstance(d, dict)
            if target_coordinate_system in d:
                # Because of how spatialdata._core.operations.transform._adjust_transformations() is implemented, we
                # know that the transformation tt below is a sequence of transformations with two transformations,
                # with the second transformation equal to t.transformations[0]. Let's remove the second transformation.
                # since target_coordinate_system is in d, we have that t is a Sequence with only one transformation.
                assert isinstance(t, Sequence)
                assert len(t.transformations) == 1
                seq = get_transformation(transformed, to_coordinate_system=target_coordinate_system)
                assert isinstance(seq, Sequence)
                assert len(seq.transformations) == 2
                assert seq.transformations[1] is t.transformations[0]
                new_tt = seq.transformations[0]
                set_transformation(transformed, new_tt, target_coordinate_system)
        return transformed

    def transform_to_coordinate_system(
        self,
        target_coordinate_system: str,
        maintain_positioning: bool = False,
    ) -> SpatialData:
        """
        Transform the SpatialData to a given coordinate system.

        Parameters
        ----------
        target_coordinate_system
            The target coordinate system.
        maintain_positioning
            Default False (most common use case). If True, the data will be transformed but a transformation will be
            added so that the positioning of the data in the target coordinate system will not change. If you want to
            align datasets to a common coordinate system you should use the default value.

        Returns
        -------
        The transformed SpatialData.
        """
        sdata = self.filter_by_coordinate_system(target_coordinate_system, filter_tables=False)
        elements: dict[str, dict[str, SpatialElement]] = {}
        for element_type, element_name, element in sdata._gen_elements():
            if element_type != "tables":
                transformed = sdata.transform_element_to_coordinate_system(
                    element, target_coordinate_system, maintain_positioning=maintain_positioning
                )
                if element_type not in elements:
                    elements[element_type] = {}
                elements[element_type][element_name] = transformed
        return SpatialData(**elements, tables=sdata.tables)

    def is_self_contained(self: SpatialData) -> bool:
        """
        Check if a SpatialData object is self-contained; self-contained objects have a simpler disk storage layout.

        A SpatialData object is said to be self-contained if all its SpatialElements or AnnData tables are
        self-contained. A SpatialElement or AnnData table is said to be self-contained when it does not depend on a
        Dask computational graph (i.e. it is not "lazy") or when it is Dask-backed and each file that is read in the
        Dask computational graph is contained within the Zarr store associated with the SpatialElement.

        Currently, Points, Labels and Images are always represented lazily, while Shapes and Tables are always
        in-memory. Therefore, the latter are always self-contained.

        Printing a SpatialData object will show if any of its elements are not self-contained.

        Returns
        -------
        A boolean value indicating whether the SpatialData object is self-contained.

        Notes
        -----
        Generally, it is preferred to work with self-contained SpatialData objects; working with non-self-contained
        SpatialData objects is possible but requires more care when performing IO operations:

            1.  Non-self-contained elements depend on files outside the Zarr store associated with the SpatialData
                object. Therefore, changes on these external files (such as deletion), will be reflected in the
                SpatialData object.
            2.  When calling `write_element()` and `write_element()` metadata, the changes will be applied to the Zarr
                store associated with the SpatialData object, not on the external files.
        """
        from spatialdata._io._utils import is_element_self_contained

        if self.path is None:
            return True

        self_contained = True
        for element_type, element_name, element in self.gen_elements():
            element_path = Path(self.path) / element_type / element_name
            element_is_self_contained = is_element_self_contained(element=element, element_path=element_path)
            self_contained = self_contained and element_is_self_contained
        return self_contained

    def _validate_can_safely_write_to_path(
        self,
        file_path: str | Path,
        overwrite: bool = False,
        saving_an_element: bool = False,
    ) -> None:
        from spatialdata._io._utils import _backed_elements_contained_in_path, _is_subfolder

        if isinstance(file_path, str):
            file_path = Path(file_path)

        if not isinstance(file_path, Path):
            raise ValueError(f"file_path must be a string or a Path object, type(file_path) = {type(file_path)}.")

        if os.path.exists(file_path):
            if parse_url(file_path, mode="r") is None:
                raise ValueError(
                    "The target file path specified already exists, and it has been detected to not be a Zarr store. "
                    "Overwriting non-Zarr stores is not supported to prevent accidental data loss."
                )
            if not overwrite:
                raise ValueError("The Zarr store already exists. Use `overwrite=True` to try overwriting the store.")
            if any(_backed_elements_contained_in_path(path=file_path, object=self)):
                raise ValueError(
                    "The file path specified is a parent directory of one or more files used for backing for one or "
                    "more elements in the SpatialData object. Please save the data to a different location."
                )
            if self.path is not None and (
                _is_subfolder(parent=self.path, child=file_path) or _is_subfolder(parent=file_path, child=self.path)
            ):
                if saving_an_element and _is_subfolder(parent=self.path, child=file_path):
                    raise ValueError(
                        "Currently, overwriting existing elements is not supported. It is recommended to manually save "
                        "the element in a different location and then eventually copy it to the original desired "
                        "location. Deleting the old element and saving the new element to the same location is not "
                        "advised because data loss may occur if the execution is interrupted during writing."
                    )
                raise ValueError(
                    "The file path specified either contains either is contained in the one used for backing. "
                    "Currently, overwriting the backing Zarr store is not supported to prevent accidental data loss "
                    "that could occur if the execution is interrupted during writing. Please save the data to a "
                    "different location."
                )

    def write(
        self,
        file_path: str | Path,
        overwrite: bool = False,
        consolidate_metadata: bool = True,
    ) -> None:
        if isinstance(file_path, str):
            file_path = Path(file_path)
        self._validate_can_safely_write_to_path(file_path, overwrite=overwrite)

        store = parse_url(file_path, mode="w").store
        _ = zarr.group(store=store, overwrite=overwrite)
        store.close()

        for element_type, element_name, element in self.gen_elements():
            self._write_element(
                element=element,
                zarr_container_path=file_path,
                element_type=element_type,
                element_name=element_name,
                overwrite=False,
            )

        if self.path != file_path:
            old_path = self.path
            self.path = file_path
            logger.info(f"The Zarr backing store has been changed from {old_path} the new file path: {file_path}")

        if consolidate_metadata:
            self.write_consolidated_metadata()

    def _write_element(
        self,
        element: SpatialElement | AnnData,
        zarr_container_path: Path,
        element_type: str,
        element_name: str,
        overwrite: bool,
    ) -> None:
        if not isinstance(zarr_container_path, Path):
            raise ValueError(
                f"zarr_container_path must be a Path object, type(zarr_container_path) = {type(zarr_container_path)}."
            )
        file_path_of_element = zarr_container_path / element_type / element_name
        self._validate_can_safely_write_to_path(
            file_path=file_path_of_element, overwrite=overwrite, saving_an_element=True
        )

        root_group, element_type_group, _ = self._get_groups_for_element(
            zarr_path=zarr_container_path, element_type=element_type, element_name=element_name
        )
        from spatialdata._io import write_image, write_labels, write_points, write_shapes, write_table

        if element_type == "images":
            write_image(image=element, group=element_type_group, name=element_name)
        elif element_type == "labels":
            write_labels(labels=element, group=root_group, name=element_name)
        elif element_type == "points":
            write_points(points=element, group=element_type_group, name=element_name)
        elif element_type == "shapes":
            write_shapes(shapes=element, group=element_type_group, name=element_name)
        elif element_type == "tables":
            write_table(table=element, group=element_type_group, name=element_name)
        else:
            raise ValueError(f"Unknown element type: {element_type}")

    def write_element(self, element_name: str, overwrite: bool = False) -> None:
        """
        Write a single element to the Zarr store used for backing.

        The element must already be present in the SpatialData object.

        Parameters
        ----------
        element_name
            The name of the element to write.
        overwrite
            If True, overwrite the element if it already exists.
        """
        self._validate_element_names_are_unique()
        element = self.get(element_name)
        if element is None:
            raise ValueError(f"Element with name {element_name} not found in SpatialData object.")

        if self.path is None:
            raise ValueError(
                "The SpatialData object appears not to be backed by a Zarr storage, so elements cannot be written to "
                "disk."
            )

        element_type = None
        for _element_type, _element_name, _ in self.gen_elements():
            if _element_name == element_name:
                element_type = _element_type
                break
        if element_type is None:
            raise ValueError(f"Element with name {element_name} not found in SpatialData object.")

        self._write_element(
            element=element,
            zarr_container_path=self.path,
            element_type=element_type,
            element_name=element_name,
            overwrite=overwrite,
        )

    def write_consolidated_metadata(self) -> None:
        store = parse_url(self.path, mode="r+").store
        # consolidate metadata to more easily support remote reading bug in zarr. In reality, 'zmetadata' is written
        # instead of '.zmetadata' see discussion https://github.com/zarr-developers/zarr-python/issues/1121
        zarr.consolidate_metadata(store, metadata_key=".zmetadata")
        store.close()

    def write_transformations(self, element_name: str | None = None) -> None:
        """
        Write transformations to disk for a single element, or for all elements, without rewriting the data.

        Parameters
        ----------
        element_name
            The name of the element to write. If None, write the transformations of all elements.
        """
        from spatialdata._io._utils import is_element_self_contained
        from spatialdata.transformations.operations import get_transformation

        # recursively write the transformation for all the SpatialElement
        if element_name is None:
            for _, element_name, _ in self._gen_elements():
                self.write_transformations(element_name)
            return

        # check the element exists in the SpatialData object
        element = self.get(element_name)
        if element is None:
            raise ValueError(
                "Cannot save the transformation to the element as it has not been found in the SpatialData object"
            )

        # check there is a Zarr store for the SpatialData object
        if self.path is None:
            warnings.warn(
                "The SpatialData object appears not to be backed by a Zarr storage, so transformations cannot be "
                "written",
                UserWarning,
                stacklevel=2,
            )
            return

        transformations = get_transformation(element, get_all=True)
        assert isinstance(transformations, dict)

        # (redundant) safety check, we should always expect no duplicate names for elements
        located = self.locate_element(element)
        if len(located) != 1:
            raise ValueError(
                f"Expected to find exactly one element with name {element_name}, but found {len(located)} elements."
            )
        path = located[0]
        found_element_type, found_element_name = path.split("/")

        # check if the element exists in the Zarr storage
        if not self._group_for_element_exists(
            zarr_path=Path(self.path), element_type=found_element_type, element_name=found_element_name
        ):
            warnings.warn(
                f"Not saving the transformation to element {found_element_type}/{found_element_name} as it is"
                " not found in Zarr storage. You may choose to call write_element() first.",
                UserWarning,
                stacklevel=2,
            )
            return

        # warn the users if the element is not self-contained, that is, it is Dask-backed by files outside the Zarr
        # group for the element
        element_zarr_path = Path(self.path) / found_element_type / found_element_name
        if not is_element_self_contained(element=element, element_path=element_zarr_path):
            logger.info(
                f"Element {found_element_type}/{found_element_name} is not self-contained. The transformation will be"
                " saved to the Zarr group of the element in the SpatialData Zarr store. The data outside the element "
                "Zarr group will not be affected."
            )

        _, _, element_group = self._get_groups_for_element(
            zarr_path=Path(self.path), element_type=found_element_type, element_name=found_element_name
        )
        axes = get_axes_names(element)
        if isinstance(element, (SpatialImage, MultiscaleSpatialImage)):
            from spatialdata._io._utils import (
                overwrite_coordinate_transformations_raster,
            )

            overwrite_coordinate_transformations_raster(group=element_group, axes=axes, transformations=transformations)
        elif isinstance(element, (DaskDataFrame, GeoDataFrame, AnnData)):
            from spatialdata._io._utils import (
                overwrite_coordinate_transformations_non_raster,
            )

            overwrite_coordinate_transformations_non_raster(
                group=element_group, axes=axes, transformations=transformations
            )
        else:
            raise ValueError(f"Unknown element type {type(element)}")

    def write_metadata(self, element_name: str | None = None, consolidate_metadata: bool | None = None) -> None:
        """
        Write the metadata of a single element, or of all elements, to the Zarr store, without rewriting the data.

        Currently only the transformations and the consolidated metadata can be re-written without re-writing the data.

        Future versions of SpatialData will support writing the following metadata without requiring a rewrite of the
        data:

            - .uns['spatialdata_attrs'] metadata for AnnData;
            - .attrs['spatialdata_attrs'] metadata for DaskDataFrame;
            - OMERO metadata for the channel name of images.

        Parameters
        ----------
        element_name
            The name of the element to write. If None, write the metadata of all elements.
        consolidate_metadata
            If True, consolidate the metadata to more easily support remote reading. By default write the metadata
            only if the metadata was already consolidated.

        Notes
        -----
        When using the methods `write()` and `write_element()`, the metadata is written automatically.
        """
        self.write_transformations(element_name)
        # TODO: write .uns['spatialdata_attrs'] metadata for AnnData.
        # TODO: write .attrs['spatialdata_attrs'] metadata for DaskDataFrame.
        # TODO: write omero metadata for the channel name of images.

        if consolidate_metadata is None:
            store = parse_url(self.path, mode="r").store
            if "zmetadata" in store:
                consolidate_metadata = True
            store.close()
        if consolidate_metadata:
            self.write_consolidated_metadata()

    @property
    def tables(self) -> Tables:
        """
        Return tables dictionary.

        Returns
        -------
        dict[str, AnnData]
            Either the empty dictionary or a dictionary with as values the strings representing the table names and
            as values the AnnData tables themselves.
        """
        return self._tables

    @tables.setter
    def tables(self, shapes: dict[str, GeoDataFrame]) -> None:
        """Set tables."""
        self._shared_keys = self._shared_keys - set(self._tables.keys())
        self._tables = Tables(shared_keys=self._shared_keys)
        for k, v in shapes.items():
            self._tables[k] = v

    @property
    def table(self) -> None | AnnData:
        """
        Return table with name table from tables if it exists.

        Returns
        -------
        The table.
        """
        warnings.warn(
            "Table accessor will be deprecated with SpatialData version 0.1, use sdata.tables instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        # Isinstance will still return table if anndata has 0 rows.
        if isinstance(self.tables.get("table"), AnnData):
            return self.tables["table"]
        return None

    @table.setter
    def table(self, table: AnnData) -> None:
        warnings.warn(
            "Table setter will be deprecated with SpatialData version 0.1, use tables instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        TableModel().validate(table)
        if self.tables.get("table") is not None:
            raise ValueError("The table already exists. Use del sdata.tables['table'] to remove it first.")
        self.tables["table"] = table

    @table.deleter
    def table(self) -> None:
        """Delete the table."""
        warnings.warn(
            "del sdata.table will be deprecated with SpatialData version 0.1, use del sdata.tables['table'] instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        if self.tables.get("table"):
            del self.tables["table"]
        else:
            # More informative than the error in the zarr library.
            raise KeyError("table with name 'table' not present in the SpatialData object.")

    @staticmethod
    def read(file_path: Path | str, selection: tuple[str] | None = None) -> SpatialData:
        """
        Read a SpatialData object from a Zarr storage (on-disk or remote).

        Parameters
        ----------
        file_path
            The path or URL to the Zarr storage.
        selection
            The elements to read (images, labels, points, shapes, table). If None, all elements are read.

        Returns
        -------
        The SpatialData object.
        """
        from spatialdata import read_zarr

        return read_zarr(file_path, selection=selection)

    def add_image(
        self,
        name: str,
        image: SpatialImage | MultiscaleSpatialImage,
        storage_options: JSONDict | list[JSONDict] | None = None,
        overwrite: bool = False,
    ) -> None:
        _error_message_add_element()

    def add_labels(
        self,
        name: str,
        labels: SpatialImage | MultiscaleSpatialImage,
        storage_options: JSONDict | list[JSONDict] | None = None,
        overwrite: bool = False,
    ) -> None:
        _error_message_add_element()

    def add_points(
        self,
        name: str,
        points: DaskDataFrame,
        overwrite: bool = False,
    ) -> None:
        _error_message_add_element()

    def add_shapes(
        self,
        name: str,
        shapes: GeoDataFrame,
        overwrite: bool = False,
    ) -> None:
        _error_message_add_element()

    @property
    def images(self) -> Images:
        """Return images as a Dict of name to image data."""
        return self._images

    @images.setter
    def images(self, images: dict[str, Raster_T]) -> None:
        """Set images."""
        self._shared_keys = self._shared_keys - set(self._images.keys())
        self._images = Images(shared_keys=self._shared_keys)
        for k, v in images.items():
            self._images[k] = v

    @property
    def labels(self) -> Labels:
        """Return labels as a Dict of name to label data."""
        return self._labels

    @labels.setter
    def labels(self, labels: dict[str, Raster_T]) -> None:
        """Set labels."""
        self._shared_keys = self._shared_keys - set(self._labels.keys())
        self._labels = Labels(shared_keys=self._shared_keys)
        for k, v in labels.items():
            self._labels[k] = v

    @property
    def points(self) -> Points:
        """Return points as a Dict of name to point data."""
        return self._points

    @points.setter
    def points(self, points: dict[str, DaskDataFrame]) -> None:
        """Set points."""
        self._shared_keys = self._shared_keys - set(self._points.keys())
        self._points = Points(shared_keys=self._shared_keys)
        for k, v in points.items():
            self._points[k] = v

    @property
    def shapes(self) -> Shapes:
        """Return shapes as a Dict of name to shape data."""
        return self._shapes

    @shapes.setter
    def shapes(self, shapes: dict[str, GeoDataFrame]) -> None:
        """Set shapes."""
        self._shared_keys = self._shared_keys - set(self._shapes.keys())
        self._shapes = Shapes(shared_keys=self._shared_keys)
        for k, v in shapes.items():
            self._shapes[k] = v

    @property
    def coordinate_systems(self) -> list[str]:
        from spatialdata.transformations.operations import get_transformation

        all_cs = set()
        gen = self._gen_spatial_element_values()
        for obj in gen:
            transformations = get_transformation(obj, get_all=True)
            assert isinstance(transformations, dict)
            for cs in transformations:
                all_cs.add(cs)
        return list(all_cs)

    def _non_empty_elements(self) -> list[str]:
        """Get the names of the elements that are not empty.

        Returns
        -------
        non_empty_elements
            The names of the elements that are not empty.
        """
        all_elements = ["images", "labels", "points", "shapes", "tables"]
        return [
            element
            for element in all_elements
            if (getattr(self, element) is not None) and (len(getattr(self, element)) > 0)
        ]

    def __repr__(self) -> str:
        return self._gen_repr()

    def _gen_repr(
        self,
    ) -> str:
        """
        Generate a string representation of the SpatialData object.

        Returns
        -------
            The string representation of the SpatialData object.
        """
        from spatialdata._utils import _natural_keys

        def rreplace(s: str, old: str, new: str, occurrence: int) -> str:
            li = s.rsplit(old, occurrence)
            return new.join(li)

        def h(s: str) -> str:
            return hashlib.md5(repr(s).encode()).hexdigest()

        descr = "SpatialData object"
        if self.path is not None:
            descr += f", with associated Zarr store: {self.path.resolve()}"

        non_empty_elements = self._non_empty_elements()
        last_element_index = len(non_empty_elements) - 1
        for attr_index, attr in enumerate(non_empty_elements):
            last_attr = attr_index == last_element_index
            attribute = getattr(self, attr)

            descr += f"\n{h('level0')}{attr.capitalize()}"

            unsorted_elements = attribute.items()
            sorted_elements = sorted(unsorted_elements, key=lambda x: _natural_keys(x[0]))
            for k, v in sorted_elements:
                descr += f"{h('empty_line')}"
                descr_class = v.__class__.__name__
                if attr == "shapes":
                    descr += f"{h(attr + 'level1.1')}{k!r}: {descr_class} " f"shape: {v.shape} (2D shapes)"
                elif attr == "points":
                    length: int | None = None
                    if len(v.dask.layers) == 1:
                        name, layer = v.dask.layers.items().__iter__().__next__()
                        if "read-parquet" in name:
                            t = layer.creation_info["args"]
                            assert isinstance(t, tuple)
                            assert len(t) == 1
                            parquet_file = t[0]
                            table = read_parquet(parquet_file)
                            length = len(table)
                        else:
                            # length = len(v)
                            length = None
                    else:
                        length = None

                    n = len(get_axes_names(v))
                    dim_string = f"({n}D points)"

                    assert len(v.shape) == 2
                    if length is not None:
                        shape_str = f"({length}, {v.shape[1]})"
                    else:
                        shape_str = (
                            "("
                            + ", ".join([str(dim) if not isinstance(dim, Delayed) else "<Delayed>" for dim in v.shape])
                            + ")"
                        )
                    descr += f"{h(attr + 'level1.1')}{k!r}: {descr_class} " f"with shape: {shape_str} {dim_string}"
                elif attr == "tables":
                    descr += f"{h(attr + 'level1.1')}{k!r}: {descr_class} {v.shape}"
                else:
                    if isinstance(v, SpatialImage):
                        descr += f"{h(attr + 'level1.1')}{k!r}: {descr_class}[{''.join(v.dims)}] {v.shape}"
                    elif isinstance(v, MultiscaleSpatialImage):
                        shapes = []
                        dims: str | None = None
                        for pyramid_level in v:
                            dataset_names = list(v[pyramid_level].keys())
                            assert len(dataset_names) == 1
                            dataset_name = dataset_names[0]
                            vv = v[pyramid_level][dataset_name]
                            shape = vv.shape
                            if dims is None:
                                dims = "".join(vv.dims)
                            shapes.append(shape)
                        descr += f"{h(attr + 'level1.1')}{k!r}: {descr_class}[{dims}] " f"{', '.join(map(str, shapes))}"
                    else:
                        raise TypeError(f"Unknown type {type(v)}")
            if last_attr is True:
                descr = descr.replace(h("empty_line"), "\n  ")
            else:
                descr = descr.replace(h("empty_line"), "\n│ ")

        descr = rreplace(descr, h("level0"), "└── ", 1)
        descr = descr.replace(h("level0"), "├── ")

        for attr in ["images", "labels", "points", "tables", "shapes"]:
            descr = rreplace(descr, h(attr + "level1.1"), "    └── ", 1)
            descr = descr.replace(h(attr + "level1.1"), "    ├── ")

        from spatialdata.transformations.operations import get_transformation

        descr += "\nwith coordinate systems:\n"
        coordinate_systems = self.coordinate_systems.copy()
        coordinate_systems.sort(key=_natural_keys)
        for i, cs in enumerate(coordinate_systems):
            descr += f"    ▸ {cs!r}"
            gen = self._gen_elements()
            elements_in_cs: dict[str, list[str]] = {}
            for k, name, obj in gen:
                if not isinstance(obj, AnnData):
                    transformations = get_transformation(obj, get_all=True)
                    assert isinstance(transformations, dict)
                    target_css = transformations.keys()
                    if cs in target_css:
                        if k not in elements_in_cs:
                            elements_in_cs[k] = []
                        elements_in_cs[k].append(name)
            for element_names in elements_in_cs.values():
                element_names.sort(key=_natural_keys)
            if len(elements_in_cs) > 0:
                elements = ", ".join(
                    [
                        f"{element_name} ({element_type.capitalize()})"
                        for element_type, element_names in elements_in_cs.items()
                        for element_name in element_names
                    ]
                )
                descr += f", with elements:\n        {elements}"
            if i < len(coordinate_systems) - 1:
                descr += "\n"

        from spatialdata._io._utils import get_dask_backing_files, is_element_self_contained

        if not self.is_self_contained():
            assert self.path is not None
            descr += "\nwith the following Dask-backed elements not being self-contained:\n"
            for element_type, element_name, element in self.gen_elements():
                element_path = Path(self.path) / element_type / element_name
                if not is_element_self_contained(element, element_path):
                    backing_files = ", ".join(get_dask_backing_files(element))
                    descr += f"    ▸ {element_name}: {backing_files} \n"
        return descr

    def _gen_spatial_element_values(self) -> Generator[SpatialElement, None, None]:
        """
        Generate spatial element objects contained in the SpatialData instance.

        Returns
        -------
        Generator[SpatialElement, None, None]
            A generator that yields spatial element objects contained in the SpatialData instance.

        """
        for element_type in ["images", "labels", "points", "shapes"]:
            d = getattr(SpatialData, element_type).fget(self)
            yield from d.values()

    def _gen_elements(
        self, include_table: bool = False
    ) -> Generator[tuple[str, str, SpatialElement | AnnData], None, None]:
        """
        Generate elements contained in the SpatialData instance.

        Parameters
        ----------
        include_table
            Whether to also generate table elements.

        Returns
        -------
        A generator object that returns a tuple containing the type of the element, its name, and the element
        itself.
        """
        element_types = ["images", "labels", "points", "shapes"]
        if include_table:
            element_types.append("tables")
        for element_type in element_types:
            d = getattr(SpatialData, element_type).fget(self)
            for k, v in d.items():
                yield element_type, k, v

    def gen_spatial_elements(self) -> Generator[tuple[str, str, SpatialElement], None, None]:
        """
        Generate spatial elements within the SpatialData object.

        This method generates spatial elements (images, labels, points and shapes).

        Returns
        -------
        A generator that yields tuples containing the element_type (string), name, and SpatialElement objects
        themselves.
        """
        return self._gen_elements()

    def gen_elements(self) -> Generator[tuple[str, str, SpatialElement | AnnData], None, None]:
        """
        Generate elements within the SpatialData object.

        This method generates elements in the SpatialData object (images, labels, points, shapes and tables)

        Returns
        -------
        A generator that yields tuples containing the name, description, and element objects themselves.
        """
        return self._gen_elements(include_table=True)

    def _validate_element_names_are_unique(self) -> None:
        """
        Validate that the element names are unique.

        Raises
        ------
        ValueError
            If the element names are not unique.
        """
        element_names = set()
        for _, element_name, _ in self.gen_elements():
            if element_name in element_names:
                raise ValueError(f"Element name {element_name!r} is not unique.")
            element_names.add(element_name)

    def _find_element(self, element_name: str) -> tuple[str, str, SpatialElement | AnnData]:
        """
        Retrieve SpatialElement or Table from the SpatialData instance matching element_name.

        Parameters
        ----------
        element_name
            The name of the element to find.

        Returns
        -------
        A tuple containing the element type, element name, and the retrieved element itself.

        Notes
        -----
        Valid types are "images", "labels", "points", "shapes", and "tables".

        Raises
        ------
        KeyError
            If the element with the given name cannot be found.
        """
        found = []
        for element_type, element_name_, element in self.gen_elements():
            if element_name_ == element_name:
                found.append((element_type, element_name_, element))

        if len(found) == 0:
            raise KeyError(f"Could not find element with name {element_name!r}")

        if len(found) > 1:
            raise ValueError(f"Found multiple elements with name {element_name!r}")

        return found[0]

    @classmethod
    @deprecation_alias(table="tables")
    def init_from_elements(
        cls, elements: dict[str, SpatialElement], tables: AnnData | dict[str, AnnData] | None = None
    ) -> SpatialData:
        """
        Create a SpatialData object from a dict of named elements and an optional table.

        Parameters
        ----------
        elements
            A dict of named elements.
        tables
            An optional table or dictionary of tables

        Returns
        -------
        The SpatialData object.
        """
        elements_dict: dict[str, SpatialElement] = {}
        for name, element in elements.items():
            model = get_model(element)
            if model in [Image2DModel, Image3DModel]:
                element_type = "images"
            elif model in [Labels2DModel, Labels3DModel]:
                element_type = "labels"
            elif model == PointsModel:
                element_type = "points"
            else:
                assert model == ShapesModel
                element_type = "shapes"
            elements_dict.setdefault(element_type, {})[name] = element
        return cls(**elements_dict, tables=tables)

    def subset(
        self, element_names: list[str], filter_tables: bool = True, include_orphan_tables: bool = False
    ) -> SpatialData:
        """
        Subset the SpatialData object.

        Parameters
        ----------
        element_names
            The names of the element_names to subset. If the element_name is the name of a table, this table would be
            completely included in the subset even if filter_table is True.
        filter_table
            If True (default), the table is filtered to only contain rows that are annotating regions
            contained within the element_names.
        include_orphan_tables
            If True (not default), include tables that do not annotate SpatialElement(s). Only has an effect if
            filter_tables is also set to True.

        Returns
        -------
        The subsetted SpatialData object.
        """
        elements_dict: dict[str, SpatialElement] = {}
        names_tables_to_keep: set[str] = set()
        for element_type, element_name, element in self._gen_elements(include_table=True):
            if element_name in element_names:
                if element_type != "tables":
                    elements_dict.setdefault(element_type, {})[element_name] = element
                else:
                    names_tables_to_keep.add(element_name)
        tables = self._filter_tables(
            names_tables_to_keep,
            filter_tables,
            "elements",
            include_orphan_tables,
            elements_dict=elements_dict,
        )
        return SpatialData(**elements_dict, tables=tables)

    def __getitem__(self, item: str) -> SpatialElement:
        """
        Return the element with the given name.

        Parameters
        ----------
        item
            The name of the element to return.

        Returns
        -------
        The element.
        """
        _, _, element = self._find_element(item)
        return element

    def __contains__(self, key: str) -> bool:
        element_dict = {
            element_name: element_value for _, element_name, element_value in self._gen_elements(include_table=True)
        }
        return key in element_dict

    def get(self, key: str, default_value: SpatialElement | AnnData | None = None) -> SpatialElement | AnnData | None:
        """
        Get element from SpatialData object based on corresponding name.

        Parameters
        ----------
        key
            The key to lookup in the spatial elements.
        default_value
            The default value (a SpatialElement or a table) to return if the key is not found. Default is None.

        Returns
        -------
        The SpatialData element associated with the given key, if found. Otherwise, the default value is returned.
        """
        for _, element_name_, element in self.gen_elements():
            if element_name_ == key:
                return element
        else:
            return default_value

    def __setitem__(self, key: str, value: SpatialElement | AnnData) -> None:
        """
        Add the element to the SpatialData object.

        Parameters
        ----------
        key
            The name of the element.
        value
            The element.
        """
        schema = get_model(value)
        if schema in (Image2DModel, Image3DModel):
            self.images[key] = value
        elif schema in (Labels2DModel, Labels3DModel):
            self.labels[key] = value
        elif schema == PointsModel:
            self.points[key] = value
        elif schema == ShapesModel:
            self.shapes[key] = value
        elif schema == TableModel:
            self.tables[key] = value
        else:
            raise TypeError(f"Unknown element type with schema: {schema!r}.")


class QueryManager:
    """Perform queries on SpatialData objects."""

    def __init__(self, sdata: SpatialData):
        self._sdata = sdata

    def bounding_box(
        self,
        axes: tuple[str, ...],
        min_coordinate: ArrayLike,
        max_coordinate: ArrayLike,
        target_coordinate_system: str,
        filter_table: bool = True,
    ) -> SpatialData:
        """
        Perform a bounding box query on the SpatialData object.

        Please see
        :func:`spatialdata.bounding_box_query` for the complete docstring.
        """
        from spatialdata._core.query.spatial_query import bounding_box_query

        return bounding_box_query(  # type: ignore[return-value]
            self._sdata,
            axes=axes,
            min_coordinate=min_coordinate,
            max_coordinate=max_coordinate,
            target_coordinate_system=target_coordinate_system,
            filter_table=filter_table,
        )

    def polygon(
        self,
        polygon: Polygon | MultiPolygon,
        target_coordinate_system: str,
        filter_table: bool = True,
    ) -> SpatialData:
        """
        Perform a polygon query on the SpatialData object.

        Please see
        :func:`spatialdata.polygon_query` for the complete docstring.
        """
        from spatialdata._core.query.spatial_query import polygon_query

        return polygon_query(  # type: ignore[return-value]
            self._sdata,
            polygon=polygon,
            target_coordinate_system=target_coordinate_system,
            filter_table=filter_table,
        )

    def __call__(self, request: BaseSpatialRequest, **kwargs) -> SpatialData:  # type: ignore[no-untyped-def]
        from spatialdata._core.query.spatial_query import BoundingBoxRequest

        if not isinstance(request, BoundingBoxRequest):
            raise TypeError("unknown request type")
        # TODO: request doesn't contain filter_table. If the user doesn't specify this in kwargs, it will be set
        #  to it's default value. This could be a bit unintuitive and
        #  we may want to change make things more explicit.
        return self.bounding_box(**request.to_dict(), **kwargs)
