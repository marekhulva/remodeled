"""
Component base class.

Every component implements:
  - preferred_size() -> (w, h) in inches  : its natural visual size
  - render(x, y, w, h) -> [shape dicts]   : draw itself into the given box

Shapes returned are plain dicts consumed by both the canvas (Fabric.js)
and PPTX renderers. Coordinates are in inches; the canvas is infinite, so
components render at preferred_size() and never get squeezed.
"""


class Component:
    # Layout metadata used by the placement engine. Subclasses override
    # to declare their importance and how they should be placed.
    #
    # priority: 1 (critical, never shrunk) ... 5 (optional, shrinks first).
    #   Used by the constraint solver to decide who shrinks when the
    #   diagram doesn't fit.
    #
    # placement: 'anchor' | 'fill' | 'free'
    #   - 'anchor': fixed natural position (sites, AGP zone)
    #   - 'fill':   sized to fit available space (SaaS app cards)
    #   - 'free':   layout engine decides based on context (Unity card)
    #
    # shrink_x, shrink_y: 0.0 = rigid, 1.0 = freely shrinkable. The solver
    # multiplies (preferred - min) by this elasticity when distributing
    # the deficit across components in the same priority tier.
    priority = 1
    placement = 'anchor'
    shrink_x = 0.0
    shrink_y = 0.0
    # zone: which canvas region this component belongs to.
    #   'main_row'    → packed left-to-right in the primary site row
    #   'right_panel' → placed to the right of the main row (AGP zone)
    #   'float'       → placed in best available free space (negative-space finder)
    #   'header'      → centered above the main row (Unity, CommvaultCloud)
    zone = 'main_row'
    # layout_group: optional tag for grouping special float components.
    #   'saas_pair' → SaaSAppCard items placed as a column grid with paired AGP cards.
    #   None        → individual float placement via negative-space finder.
    layout_group = None
    # agp_source: routing intent for AGP source lines.
    #   'always'   → always draws source lines to every AGP (on-prem, cloud sites)
    #   'never'    → never draws source lines
    #   'explicit' → only draws when named in flow graph source_site_ids
    agp_source = 'never'

    def routing_anchors(self, x, y, w, h):
        """Named anchor points for connection routing.
        Engine queries these instead of isinstance checks.
        Override to expose internal points (storage_bottom, cloud_entry, etc.).
        Returns dict of name → (abs_x, abs_y).
        """
        return {
            'top_center':    (x + w / 2, y),
            'bottom_center': (x + w / 2, y + h),
            'left_center':   (x,         y + h / 2),
            'right_center':  (x + w,     y + h / 2),
        }

    def copy_badge_anchor(self, x, y, w, h):
        """Return (bx, by) for copy-number badge circle, or None.
        Default: None. OnPremSite overrides to return storage media center.
        """
        return None

    def preferred_size(self):
        raise NotImplementedError(f'{type(self).__name__}.preferred_size()')

    def min_size(self):
        """Smallest size at which the component is still readable.
        Default is preferred_size — components that can shrink override."""
        return self.preferred_size()

    def size_for(self, max_w, max_h):
        """Return the (w, h) the component will actually use when constrained
        to fit inside (max_w, max_h). Must satisfy min_size <= result <=
        preferred_size. Default clamps preferred against the bounds; components
        with internal scaling override for proportional shrinking."""
        pw, ph = self.preferred_size()
        mw, mh = self.min_size()
        return (max(mw, min(pw, max_w)),
                max(mh, min(ph, max_h)))

    def render(self, x, y, w, h):
        raise NotImplementedError(f'{type(self).__name__}.render()')


# ----- Shape helpers (inches in, dict out) -----

def rect(x, y, w, h, fill=None, stroke=None, sw=1, radius=0, gradient=None):
    """Rectangle.

    radius > 0 (in inches) → rounded corners.
    gradient = [from_hex, to_hex] → vertical linear gradient (top → bottom).
              Takes precedence over `fill` if both supplied.
    """
    return {'type': 'rect', 'x': round(x, 4), 'y': round(y, 4),
            'w': round(w, 4), 'h': round(h, 4),
            'fill': fill, 'stroke': stroke, 'sw': sw,
            'radius': round(radius, 4),
            'gradient': gradient}


def text(x, y, w, h, content, fs=10, color='#FFFFFF', bold=False,
         align='left', valign='top'):
    """Text shape. align = left|center|right. valign = top|middle|bottom."""
    return {'type': 'text', 'x': round(x, 4), 'y': round(y, 4),
            'w': round(w, 4), 'h': round(h, 4),
            'text': content, 'fs': fs, 'color': color,
            'bold': bold, 'align': align, 'valign': valign}


def oval(x, y, w, h, fill=None, stroke=None, sw=1,
         text_content=None, fs=7, text_color='#FFFFFF'):
    return {'type': 'oval', 'x': round(x, 4), 'y': round(y, 4),
            'w': round(w, 4), 'h': round(h, 4),
            'fill': fill, 'stroke': stroke, 'sw': sw,
            'text': text_content, 'fs': fs, 'text_color': text_color}


def image(x, y, w, h, src):
    return {'type': 'image', 'x': round(x, 4), 'y': round(y, 4),
            'w': round(w, 4), 'h': round(h, 4),
            'src': src}


def line(x1, y1, x2, y2, stroke='#5C5F6B', sw=1, dash='dash', arrow=None):
    """Line between two points. dash = 'solid' | 'dash' | 'dot'.
    arrow = None | 'end' | 'start' | 'both' — adds a triangle marker
    pointing along the line direction. Arrows render in both the
    Fabric.js canvas and the PPTX export."""
    return {'type': 'line',
            'x1': round(x1, 4), 'y1': round(y1, 4),
            'x2': round(x2, 4), 'y2': round(y2, 4),
            'stroke': stroke, 'sw': sw, 'dash': dash,
            'arrow': arrow}
