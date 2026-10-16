"""Region is collection provenance, never inferred from article language or URL."""
REGIONS = ('cn', 'intl', 'other')
CONFIGURED_REGIONS = {'中国': 'cn', '美国': 'intl', '欧洲': 'intl', '日本': 'intl'}


def normalized(value):
    return value if value in REGIONS else 'other'


def bind_lane(item, lane):
    if lane not in ('cn', 'intl'):
        raise ValueError('Unknown collection lane: ' + str(lane))
    item['region'] = lane
    item['region_provenance'] = {'kind': 'lane', 'lane': lane}


def bind_source(item, source_key, configured_region=None):
    value = CONFIGURED_REGIONS.get(configured_region, normalized(configured_region))
    item['region'] = value
    item['region_provenance'] = {'kind': 'source_config', 'source_key': source_key,
                                 'configured_region': configured_region}
