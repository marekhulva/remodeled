"""PlacementRegistry — tracks every placed component by ID.

The layout engine registers each component as it's placed.
Routing and badge phases then query the registry by ID without
needing to know what class a component is.

Usage:
    registry = PlacementRegistry()
    registry.register('site_hq', site_comp, x=0.3, y=1.1, w=2.8, h=4.2)

    # Later — routing phase:
    anchors = registry.anchors('site_hq')
    src = anchors.get('storage_bottom', anchors['bottom_center'])
"""


class PlacementRegistry:

    def __init__(self):
        # comp_id → (comp, x, y, w, h)
        self._data = {}
        # Ordered list of IDs in registration order (for badge numbering)
        self._order = []

    def register(self, comp_id, comp, x, y, w, h):
        """Record a placed component. Overwrites if comp_id already registered."""
        if comp_id not in self._data:
            self._order.append(comp_id)
        self._data[comp_id] = (comp, x, y, w, h)

    def anchors(self, comp_id):
        """Return named anchor dict for comp_id. Calls comp.routing_anchors()."""
        if comp_id not in self._data:
            return {}
        comp, x, y, w, h = self._data[comp_id]
        return comp.routing_anchors(x, y, w, h)

    def all_rects(self):
        """Return list of (x, y, w, h) for all registered components."""
        return [(x, y, w, h) for comp, x, y, w, h in self._data.values()]

    def items(self):
        """Iterate (comp_id, comp, x, y, w, h) in registration order."""
        for comp_id in self._order:
            comp, x, y, w, h = self._data[comp_id]
            yield comp_id, comp, x, y, w, h

    def get(self, comp_id):
        """Return (comp, x, y, w, h) or None."""
        return self._data.get(comp_id)

    def update_position(self, comp_id, x, y):
        """Move a registered component to a new (x, y). Used by reviewer."""
        if comp_id in self._data:
            comp, _, _, w, h = self._data[comp_id]
            self._data[comp_id] = (comp, x, y, w, h)

    def __contains__(self, comp_id):
        return comp_id in self._data

    def __len__(self):
        return len(self._data)
