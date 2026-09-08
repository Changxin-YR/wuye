"""Compatibility wrapper around the stable transactional property service."""
import re

from flask import abort

from property_service_core import *
from property_service_core import PropertyService as _CorePropertyService


class PropertyService(_CorePropertyService):
    """Keep normal list limits, but allow a bounded all-community notice batch."""

    def ids(self, key):
        if key != 'community_ids':
            return super().ids(key)
        value = self.data.get(key)
        if isinstance(value, str):
            value = [item.strip() for item in value.split(',') if item.strip()]
        if (
            not isinstance(value, list)
            or not 1 <= len(value) <= 100
            or any(isinstance(item, bool) or not re.fullmatch(r'[1-9][0-9]{0,8}', str(item)) for item in value)
        ):
            abort(400, description='请选择1—100个有效小区')
        return list(dict.fromkeys(map(int, value)))
