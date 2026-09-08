"""Compatibility wrapper around the stable transactional property service."""
import re

from flask import abort

from property_service_core import *
from property_service_core import PropertyService as _CorePropertyService


class PropertyService(_CorePropertyService):
    """Tight compatibility fixes around batch limits and notice write scope."""

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

    def do_notice(self, action):
        # A building-scoped operator may see the parent community, but that does
        # not grant community-wide write authority.  If there is exactly one
        # writable building in the requested community, treat an unspecified
        # notice target as that building.  Multiple building scopes must be
        # disambiguated instead of silently widening the audience.
        if action == 'save' and not self.data.get('id') and not self.data.get('building_id') and self.data.get('community_id'):
            cid = self.integer('community_id')
            if not self.policy.within(cid, None, True):
                building_ids = sorted({
                    int(scope.building_id)
                    for scope in self.policy.scopes
                    if scope.kind == 'building' and scope.community_id == cid and scope.building_id
                })
                if len(building_ids) > 1:
                    abort(400, description='当前账号仅有楼栋级公告权限，请明确要发布到哪一栋')
                if len(building_ids) == 1:
                    original = self.data
                    self.data = {**original, 'building_id': building_ids[0]}
                    try:
                        return super().do_notice(action)
                    finally:
                        self.data = original
        return super().do_notice(action)
