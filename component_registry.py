"""Component registry — single source of truth for type → class mapping.

Adding a new diagram component type:
  1. Create your class in components/
  2. Add one line here: 'your_type': YourClass
  3. Done. The layout engine never changes.

No isinstance checks. No if/elif chains. Just a dict lookup.
"""
from components import OnPremSite, CloudSite, SaaSSite, SaaSAppCard


COMPONENT_REGISTRY = {
    'on_prem':  OnPremSite,
    'cloud':    CloudSite,
    'saas':     SaaSSite,
    'saas_app': SaaSAppCard,
}


def build_component(d):
    """Instantiate the right component class for a scenario site entry.
    Falls back to OnPremSite if the type is unknown or missing."""
    cls = COMPONENT_REGISTRY.get(d.get('type', 'on_prem'), OnPremSite)
    return cls.from_dict(d)
