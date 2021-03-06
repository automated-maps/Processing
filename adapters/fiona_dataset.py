import logging
from functools import partial

import fiona
import pyproj
import shapely.ops as ops
from fiona.transform import transform_geom
from shapely.geometry import mapping
from shapely.geometry import MultiPolygon
from shapely.geometry import Polygon
from shapely.geometry import shape
from shapely.geometry.polygon import orient

import geoutils
import utils
from merge import merge_features
from property_transformation import get_transformed_properties
from property_transformation import PropertyMappingFailedException


def _force_geometry_2d(geometry):
    """ Convert a geometry to 2d
    """
    if geometry["type"] in ["Polygon", "MultiLineString"]:
        geometry["coordinates"] = [
            _force_linestring_2d(l) for l in geometry["coordinates"]
        ]
    elif geometry["type"] in ["LineString", "MultiPoint"]:
        geometry["coordinates"] = _force_linestring_2d(geometry["coordinates"])
    elif geometry["type"] == "Point":
        geometry["coordinates"] = geometry["coordinates"][:2]
    elif geometry["type"] == "MultiPolygon":
        geometry["coordinates"] = [
            [_force_linestring_2d(l) for l in g] for g in geometry["coordinates"]
        ]

    return geometry


def _force_linestring_2d(linestring):
    """ Convert a list of coordinates to 2d
    """
    return [c[:2] for c in linestring]


def _force_geometry_ccw(geometry):
    if geometry["type"] == "Polygon":
        return _force_polygon_ccw(geometry)
    elif geometry["type"] == "MultiPolygon":
        oriented_polygons = [
            _force_polygon_ccw({"type": "Polygon", "coordinates": g})
            for g in geometry["coordinates"]
        ]
        geometry["coordinates"] = [g["coordinates"] for g in oriented_polygons]
        return geometry
    else:
        return geometry


def _force_polygon_ccw(geometry):
    polygon = shape(geometry)
    return mapping(orient(polygon))


def _fix_geometry(geometry):
    shapely_geometry = shape(geometry)
    if not shapely_geometry.is_valid:
        buffered = shapely_geometry.buffer(0.0)
        # this will fix some invalid geometries, including bow-tie geometries, but for others it will return an empty geometry
        if buffered and (
            (type(buffered) == Polygon and buffered.exterior)
            or (type(buffered) == MultiPolygon and len(buffered.geoms) > 0)
        ):
            return mapping(buffered)
    return geometry


def read_fiona(source, prop_map, filterer=None, merge_on=None):
    """Process a fiona collection
    """
    collection = {
        "type": "FeatureCollection",
        "features": [],
        "bbox": [float("inf"), float("inf"), float("-inf"), float("-inf")],
    }
    skipped_count = 0
    failed_count = 0
    transformer = partial(
        transform_geom, source.crs, "EPSG:4326", antimeridian_cutting=True, precision=6
    )

    for feature in source:
        if filterer is not None and not filterer.keep(feature):
            skipped_count += 1
            continue
        if feature["geometry"] is None:
            logging.error("empty geometry")
            failed_count += 1
            continue
        try:
            transformed_geometry = transformer(_force_geometry_2d(feature["geometry"]))
            fixed_geometry = _fix_geometry(transformed_geometry)
            feature["geometry"] = _force_geometry_ccw(fixed_geometry)

            if merge_on:
                feature["original_properties"] = feature["properties"]
            feature["properties"] = get_transformed_properties(
                feature["properties"], prop_map
            )
            collection["features"].append(feature)
        except PropertyMappingFailedException as e:
            logging.error(str(e) + ": " + str(feature["properties"]))
            failed_count += 1
        except Exception as e:
            logging.exception("Error processing feature: " + str(feature))
            failed_count += 1

    pre_merge_count = len(collection["features"])
    if merge_on:
        collection = merge_features(
            collection, merge_on, properties_key="original_properties"
        )

    for feature in collection["features"]:
        if "original_properties" in feature:
            del feature["original_properties"]
        feature["properties"]["acres"] = geoutils.get_area_acres(feature["geometry"])
        feature["bbox"] = geoutils.get_bbox_from_geojson_feature(feature)
        if "id" in feature["properties"]:
            feature["id"] = feature["properties"]["id"]

    if len(collection["features"]) > 0:
        collection["bbox"] = geoutils.get_bbox_from_geojson(collection)

    logging.info(
        "skipped %i features, kept %i features, merged %i features, errored %i features"
        % (
            skipped_count,
            pre_merge_count,
            pre_merge_count - len(collection["features"]),
            failed_count,
        )
    )

    return collection
