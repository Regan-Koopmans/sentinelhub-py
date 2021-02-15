"""
A client interface for Sentinel Hub Catalog API
"""
import dateutil.parser

from .config import SHConfig
from .constants import CRS
from .data_collections import DataCollection
from .geometry import Geometry, CRS
from .download.sentinelhub_client import SentinelHubDownloadClient
from .sentinelhub_batch import _remove_undefined_params
from .time_utils import parse_time_interval


class SentinelHubCatalog:
    """ The main class for interacting with Sentinel Hub Catalog API

    For more details about certain endoints and parameters check
    `Catalog API documentation <https://docs.sentinel-hub.com/api/latest/api/catalog>`_.
    """
    def __init__(self, base_url=None, config=None):
        """
        :param base_url: A base URL of Sentinel Hub service specifying which service deployment to use. By default the
            one in config object will be used.
        :type base_url: str or None
        :param config: A configuration object with required parameters `sh_client_id`, `sh_client_secret`, and
            `sh_base_url` which will be used of authentication.
        :type config: SHConfig or None
        """
        self.config = config or SHConfig()

        base_url = base_url or self.config.sh_base_url
        base_url = base_url.rstrip('/')
        self.catalog_url = f'{base_url}/api/v1/catalog'

        self.client = SentinelHubDownloadClient(config=self.config)

    def get_info(self):
        """ Provides the main information that define Sentinel Hub Catalog API

        :return: A service payload with information
        :rtype: dict
        """
        return self.client.get_json(self.catalog_url)

    def get_conformance(self):
        """ Get information about specifications that this API conforms to

        :return: A service payload with information
        :rtype: dict
        """
        url = f'{self.catalog_url}/conformance'
        return self.client.get_json(url)

    def get_collections(self):
        """ Provides a list of collections that are available to a user

        :return: A list of collections with information
        :rtype: list(dict)
        """
        url = f'{self.catalog_url}/collections'
        return self.client.get_json(url, use_session=True)['collections']

    def get_collection_info(self, collection):
        """ Provides information about given collection

        :param collection: A data collection object or a collection ID
        :type collection: DataCollection or str
        :return: Information about a collection
        :rtype: dict
        """
        collection_id = self._parse_collection_id(collection)
        url = f'{self.catalog_url}/collections/{collection_id}'
        return self.client.get_json(url, use_session=True)

    def get_feature(self, collection, feature_id):
        """ Provides information about a single feature in a collection

        :param collection: A data collection object or a collection ID
        :type collection: DataCollection or str
        :param feature_id: A feature ID
        :type feature_id: str
        :return: Information about a feature in a collection
        :rtype: dict
        """
        collection_id = self._parse_collection_id(collection)
        url = f'{self.catalog_url}/collections/{collection_id}/items/{feature_id}'
        return self.client.get_json(url, use_session=True)

    def search(self, collection, *, time, bbox=None, geometry=None, ids=None, query=None, fields=None, distinct=None,
               limit=100, **kwargs):
        """ Catalog STAC search

        :param collection: A data collection object or a collection ID
        :type collection: DataCollection or str
        :param time: A time interval or a single time. It can either be a string in form  YYYY-MM-DDThh:mm:ss or
            YYYY-MM-DD or a datetime object
        :type time: (str, str) or (datetime, datetime) or str or datetime
        :param bbox: A search bounding box, it will always be reprojected to WGS 84 before being sent to the service.
        :type bbox: BBox
        :param geometry: A search geometry, it will always reprojected to WGS 84 before being sent to the service.
            This parameter is defined with parameter `intersects` at the service.
        :type geometry: Geometry
        :param ids: A list of feature ids as defined in service documentation
        :type ids: list(str)
        :param query: A STAC query described in Catalog API documentation
        :type query: dict
        :param fields: A dictionary of fields to include or exclude described in Catalog API documentation
        :type fields: dict
        :param distinct: A special query attribute described in Catalog API documentation
        :type distinct: str
        :param limit: A number of results to return per each request. At the end iterator will always provide all
            results the difference is only in how many requests it will have to make in the background.
        :type limit: int
        :param kwargs: Any other parameters that will be passed directly to the service
        """
        url = f'{self.catalog_url}/search'

        collection_id = self._parse_collection_id(collection)
        start_time, end_time = parse_time_interval(time)

        # TODO: perhaps transform into geometry before reprojecting to another CRS?
        bbox = bbox.transform(CRS.WGS84) if bbox else None
        geometry = geometry.transform(CRS.WGS84) if geometry else None

        payload = _remove_undefined_params({
            'collections': [collection_id],
            'datetime': f'{start_time}Z/{end_time}Z',
            'bbox': list(bbox) if bbox else None,
            'intersects': geometry.geojson if geometry else None,
            'ids': ids,
            'query': query,
            'fields': fields,
            'distinct': distinct,
            'limit': limit,
            **kwargs
        })

        return CatalogSearchIterator(self.client, url, payload)

    @staticmethod
    def _parse_collection_id(collection):
        """ Extracts catalog collection id from an object defining a collection
        """
        if isinstance(collection, DataCollection):
            return collection.catalog_id
        if isinstance(collection, str):
            return collection
        raise ValueError(f'Expected either a DataCollection object or a collection id string, got {collection}')


class CatalogSearchIterator:
    """ Searches a catalog with a given query and provides results

    Note that:
    - The iterator will load only as many features as needed at any moment
    - It will keep downloaded features in memory so that iterating over it again will not have to download the same
      features again.
    """
    def __init__(self, client, url, payload):
        """
        :param client: An instance of a download client object
        :type client: DownloadClient
        :param url: An URL where requests will be made
        :type url: str
        :param payload: A payload of parameters to be sent with each request
        :type payload: dict
        """
        self.client = client
        self.url = url
        self.payload = payload

        self.index = 0
        self.features = []
        self.next = None
        self.finished = False

    def __iter__(self):
        """ Iteration method

        :return: the iterator class itself
        :rtype: CatalogSearchIterator
        """
        self.index = 0
        return self

    def __next__(self):
        """ Next method

        :return: dictionary containing info about product tiles
        :rtype: dict
        """
        while self.index >= len(self.features) and not self.finished:
            self._fetch_features()

        if self.index < len(self.features):
            self.index += 1
            return self.features[self.index - 1]

        raise StopIteration

    def _fetch_features(self):
        """ Collects (more) results from the service
        """
        if self.next is not None:
            payload = {
                **self.payload,
                'next': self.next
            }
        else:
            payload = self.payload

        results = self.client.get_json(self.url, post_values=payload, use_session=True)

        new_features = results['features']
        self.features.extend(new_features)

        self.next = results['context'].get('next')
        self.finished = self.next is None or not new_features

    def get_timestamps(self):
        """ Provides features timestamps

        :return: A list of sensing times
        :rtype: list(datetime.datetime)
        """
        return [dateutil.parser.parse(feature['properties']['datetime']) for feature in self]

    def get_geometries(self):
        """ Provides features geometries

        :return: A list of geometry objects with CRS
        :rtype: list(Geometry)
        """
        return [Geometry(feature['geometry'],
                         crs=feature['geometry']['crs']['properties']['name'].rsplit(':')[-1]) for feature in self]

    def get_ids(self):
        """ Provides features IDs

        :return: A list of IDs
        :rtype: list(str)
        """
        return [feature['id'] for feature in self]
