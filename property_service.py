"""Compatibility wrapper around the stable transactional property service."""
import re

from flask import abort
from sqlalchemy import select

from permissions import effective
from property_service_core import *
from property_service_core import PropertyService as _CorePropertyService


class PropertyService(_CorePropertyService):
    """Keep normal list limits while allowing bounded multi-community notices.

    Notice write scope itself stays unchanged: callers must explicitly supply a
    target that is writable under Policy.require_scope. A building-scoped user
    is never silently upgraded or silently re-targeted by this wrapper.
    """

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

    def do_lease(self, action):
        obj, message = super().do_lease(action)
        if action == 'checkout' and obj is not None:
            relations = list(self.db.scalars(
                select(HousePerson).where(
                    HousePerson.lease_id == obj.id,
                    HousePerson.kind == 'tenant',
                )
            ))
            for relation in relations:
                relation.is_resident = False

            # Checkout ends only this tenant occupancy. A house can still have
            # effective owner/family residents, so never mark it vacant solely
            # because the lease ended. Recompute the denormalized occupancy from
            # authoritative current relationships in the same transaction.
            remaining_resident = self.db.scalar(
                select(HousePerson.id).where(
                    HousePerson.house_id == obj.house_id,
                    HousePerson.kind.in_(['owner', 'family']),
                    HousePerson.is_resident.is_(True),
                    effective(),
                ).limit(1)
            )
            house = self.db.get(House, obj.house_id)
            if house is not None:
                house.occupancy = 'owner_occupied' if remaining_resident else 'vacant'
        return obj, message
