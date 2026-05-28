"""
Layout engine — zone-based autonomous placement.

Zero isinstance checks. Zero type-string comparisons in the engine.
Components declare zone, agp_source, routing_anchors, copy_badge_anchor.
The engine reads those attributes. Adding a new component type = set
attributes on the class + one line in component_registry.py. This file
never changes.

10-phase pipeline:
  0  Parse        — build all components via COMPONENT_REGISTRY
  1  Header       — place zone='header' components centered above content
  2  PanelReserve — probe zone='right_panel' min_size, reserve canvas width
  3  MainRow      — pack zone='main_row' via constraint solver
  4  Panel        — place zone='right_panel' via score_placement
  5  Float        — place zone='float' via negative-space finder
  6  Reviewer     — detect overflow/overlap, auto-correct up to 3 passes
  7  Routing      — draw inter-site connections + AGP source lines
  8  Badges       — draw copy-number badges via copy_badge_anchor()
  9  Centering    — shift body rightward if narrower than slide
  10 Output       — return {background, content_w, content_h, shapes}
"""
import re
from collections import defaultdict

from components import AGPZone, UnityCard, COLORS
from components.saas_agp_card import SaaSAGPCard
from components.base import text, line, oval
from components.connection import Connection as _ConnStyle
from component_registry import build_component
from placement_registry import PlacementRegistry
from layout_solver import solve_rows
from negative_space import Rect, free_rects, find_best
from flow_graph import agp_source_ids

# Canvas / margin constants
MARGIN_TOP   = 1.0
MARGIN_LEFT  = 0.3
MARGIN_RIGHT = 0.3
SITE_GAP     = 0.4
AGP_GAP      = 0.5
CANVAS_W     = 13.33
CANVAS_H     = 7.5
TITLE_W      = 12.73
SLIDE_W      = 13.33
SLIDE_H      = 7.50
BADGE_SIZE   = 0.30


# ── Utilities ──────────────────────────────────────────────────────────────

def _slugify(s):
    return re.sub(r'[^a-z0-9]+', '_', s.lower()).strip('_')


def _title_shape(title):
    return text(MARGIN_LEFT, 0.33, TITLE_W, 0.57,
                title, fs=24, color=COLORS['text_primary'])


def _edge_gaps(sites_data, connections):
    n = len(sites_data)
    if n < 2:
        return []
    gaps = [SITE_GAP] * (n - 1)
    if not connections:
        return gaps
    ids = [d.get('id') or _slugify(d.get('name', '')) for d in sites_data]
    index_by_id = {sid: i for i, sid in enumerate(ids)}
    for c in connections:
        if c.get('from') not in index_by_id or c.get('to') not in index_by_id:
            continue
        a, b = sorted((index_by_id[c['from']], index_by_id[c['to']]))
        if b - a != 1:
            continue
        speed = c.get('speed', '')
        if not speed:
            continue
        pill_w = len(speed) * _ConnStyle.CHAR_W + _ConnStyle.LABEL_PAD_X * 2
        gaps[a] = max(gaps[a], pill_w + 0.20)
    return gaps


def _content_bbox(shapes):
    max_x = max_y = 0.0
    for s in shapes:
        if s['type'] == 'line':
            mx = max(s['x1'], s['x2'])
            my = max(s['y1'], s['y2'])
        else:
            mx = s.get('x', 0) + s.get('w', 0)
            my = s.get('y', 0) + s.get('h', 0)
        if mx > max_x:
            max_x = mx
        if my > max_y:
            max_y = my
    return max_x, max_y


def _rect_overlap(a, b, slack=0.02):
    return (a[0] < b[0] + b[2] - slack and a[0] + a[2] > b[0] + slack and
            a[1] < b[1] + b[3] - slack and a[1] + a[3] > b[1] + slack)


def _copy_badge(x, y, num):
    return [oval(x, y, BADGE_SIZE, BADGE_SIZE,
                 fill=COLORS['purple_primary'],
                 stroke=COLORS['text_primary'], sw=1,
                 text_content=str(num),
                 fs=10, text_color=COLORS['text_primary'])]


def _is_replication(c):
    label = (c.get('speed') or c.get('label') or '').lower()
    kind  = (c.get('kind') or '').lower()
    return 'replication' in label or kind == 'replication'


# ── AGP placement scorer ───────────────────────────────────────────────────

def _score_agp_placement(zone, site_rects, source_site_ids, site_ids,
                          min_x=None, min_y=None):
    options  = zone.size_options()
    site_ids = site_ids or [None] * len(site_rects)
    src_set  = set(source_site_ids or [])
    source_rects = [r for r, sid in zip(site_rects, site_ids) if sid in src_set]
    if not source_rects:
        source_rects = list(site_rects)

    candidates = []
    if site_rects:
        rightmost_x = max(r[0] + r[2] for r in site_rects)
        if min_x is not None:
            rightmost_x = max(rightmost_x, min_x)
        top_y = min(r[1] for r in site_rects)
        candidates.append((rightmost_x + AGP_GAP, top_y, 'right_of_sites'))
        for r in site_rects:
            sx, sy, sw, sh = r
            cand_y = sy + sh + 0.30
            if min_y is not None:
                cand_y = max(cand_y, min_y)
            candidates.append((sx, cand_y, f'below_x{sx:.1f}'))
    else:
        candidates.append((MARGIN_LEFT, MARGIN_TOP + 0.1, 'origin'))

    best = None
    best_score = -float('inf')
    for cx, cy, _ in candidates:
        for opt in options:
            sc = _score_one(cx, cy, opt['w'], opt['h'],
                            site_rects, source_rects, opt.get('shrunk'))
            if sc > best_score:
                best_score = sc
                best = (cx, cy, opt)
    return best if best is not None else (MARGIN_LEFT, MARGIN_TOP + 0.1, options[0])


def _score_one(x, y, w, h, all_sites, source_sites, shrunk):
    if x < MARGIN_LEFT - 0.01:               return -1000
    if x + w > CANVAS_W - MARGIN_RIGHT + 0.02: return -1000 + (x + w - CANVAS_W)
    if y < MARGIN_TOP - 0.5:                 return -1000
    if y + h > CANVAS_H + 0.02:             return -1000 + (y + h - CANVAS_H)
    agp_r = (x, y, w, h)
    for r in all_sites:
        if _rect_overlap(agp_r, r):
            return -500
    agp_cx = x + w / 2
    agp_cy = y + h / 2
    total_dist = sum(abs(agp_cx - (r[0]+r[2]/2)) + abs(agp_cy - (r[1]+r[3]/2))
                     for r in source_sites)
    return -total_dist - (1.0 if shrunk else 0.0)


# ── Phase helpers ──────────────────────────────────────────────────────────

def _route_connections(scenario, row_dicts, registry):
    from components.connection import Connection
    connections = scenario.get('connections', [])
    valid = [c for c in connections
             if c.get('from') in registry and c.get('to') in registry]
    if not valid:
        return []
    max_bottom = max(cy + ch
                     for _, _, cx, cy, cw, ch in registry.items())
    lane_y = max_bottom + 0.55
    shapes = []
    for c in valid:
        fa = registry.anchors(c['from'])
        ta = registry.anchors(c['to'])
        sx, sy = fa['bottom_center']
        tx, ty = ta['bottom_center']
        shapes.extend(Connection(sx, sy, tx, ty,
                                 c.get('speed', ''), bus_y=lane_y).render())
    return shapes


def _place_agp_zone(zone, site_rects, source_site_ids, site_ids, registry):
    """Position AGP zone to the RIGHT of sites, draw source lines. Zero isinstance."""
    x, y, chosen = _score_agp_placement(zone, site_rects, source_site_ids, site_ids)
    zone.apply_size_option(chosen)
    zw, zh = zone.preferred_size()

    target_anchors = zone.routing_anchors(x, y, zw, zh)
    target_x = target_anchors['cloud_entry'][0]
    target_y = target_anchors['cloud_entry'][1]
    bus_x    = x - 0.15

    stroke_kw = dict(stroke=COLORS['purple_light'], sw=1.25, dash='dash')
    line_shapes = []
    src_set = set(source_site_ids or [])

    # Shared bus_y = max bottom of all agp_source components
    source_bottoms = [cy + ch
                      for _, comp, cx, cy, cw, ch in registry.items()
                      if comp.agp_source != 'never']
    bus_y = (max(source_bottoms) + 0.25) if source_bottoms else (target_y + 0.25)

    for comp_id, comp, cx, cy, cw, ch in registry.items():
        if comp.agp_source == 'never':
            continue
        if comp.agp_source == 'explicit' and comp_id not in src_set:
            continue
        src_anchors = comp.routing_anchors(cx, cy, cw, ch)
        src_pt = src_anchors.get('storage_bottom', src_anchors['bottom_center'])
        src_x, src_y_pt = src_pt

        line_shapes.append(line(src_x, src_y_pt, src_x, bus_y, **stroke_kw))
        if abs(src_x - bus_x) > 1e-4:
            line_shapes.append(line(src_x, bus_y, bus_x, bus_y, **stroke_kw))
        if abs(bus_y - target_y) > 1e-4:
            line_shapes.append(line(bus_x, bus_y, bus_x, target_y, **stroke_kw))
        line_shapes.append(line(bus_x, target_y, target_x, target_y,
                                arrow='end', **stroke_kw))

    return line_shapes + list(zone.render(x, y, zw, zh)), x, y, zw, zh


def _place_agp_zone_bottom(zone, ax, ay, source_site_ids, registry):
    """Position AGP zone BELOW sites (bottom layout). Draw vertical source lines."""
    zone.apply_size_option(zone.size_options()[0])
    zw, zh = zone.preferred_size()

    stroke_kw = dict(stroke=COLORS['purple_light'], sw=1.25, dash='dash')
    line_shapes = []
    src_set = set(source_site_ids or [])

    # Horizontal bus line sits just above the AGP zone
    bus_y = ay - 0.25
    target_cx = ax + zw / 2

    for comp_id, comp, cx, cy, cw, ch in registry.items():
        if comp.agp_source == 'never':
            continue
        if comp.agp_source == 'explicit' and comp_id not in src_set:
            continue
        src_anchors = comp.routing_anchors(cx, cy, cw, ch)
        src_pt = src_anchors.get('storage_bottom', src_anchors['bottom_center'])
        src_x, src_y_pt = src_pt

        line_shapes.append(line(src_x, src_y_pt, src_x, bus_y, **stroke_kw))
        line_shapes.append(line(src_x, bus_y, target_cx, bus_y, **stroke_kw))
        line_shapes.append(line(target_cx, bus_y, target_cx, ay,
                                arrow='end', **stroke_kw))

    return line_shapes + list(zone.render(ax, ay, zw, zh)), ax, ay, zw, zh


def _reviewer_pass(registry, max_passes=3):
    """Detect overflow/overlap; shift lower-priority comps to free space."""
    for _ in range(max_passes):
        found = False
        all_items = list(registry.items())
        all_r = [(cid, cx, cy, cw, ch)
                 for cid, comp, cx, cy, cw, ch in all_items]
        for comp_id, comp, cx, cy, cw, ch in all_items:
            bad = (cx + cw > CANVAS_W - MARGIN_RIGHT + 0.02 or
                   cy + ch > CANVAS_H + 0.02 or
                   any(_rect_overlap((cx, cy, cw, ch), (ox, oy, ow, oh))
                       for oid, ox, oy, ow, oh in all_r if oid != comp_id))
            if not bad:
                continue
            occupied = [Rect(ox, oy, ow, oh)
                        for oid, ox, oy, ow, oh in all_r if oid != comp_id]
            canvas = Rect(MARGIN_LEFT, MARGIN_TOP,
                          CANVAS_W - MARGIN_LEFT - MARGIN_RIGHT,
                          CANVAS_H - MARGIN_TOP - 0.3)
            best = find_best(free_rects(canvas, occupied), cw, ch, prefer='top_right')
            if best:
                registry.update_position(comp_id, best.x, best.y)
                found = True
        if not found:
            break


# ── Saas app pair rows renderer ────────────────────────────────────────────

def _place_saas_app_rows(saas_app_data, agp_configs, start_x, start_y,
                          available_h=None, n_cols=1):
    """Render saas_app paired rows: [AppCard] --line--> [AGPCard]."""
    from components.saas_app_card import SaaSAppCard
    from components.agp import AirGapBreak as _AGB
    from components.base import image as _img, text as _txt
    from components.tokens import IMAGES

    shapes   = []
    LINE_GAP = 0.55
    ROW_GAP  = 0.12
    COL_GAP  = 0.30

    cloud_lookup = {c.get('cloud_provider', '').lower(): c for c in agp_configs}
    n = len(saas_app_data)
    card_probe = SaaSAppCard('_probe')
    agp_probe  = SaaSAGPCard()
    preferred_row_h = max(card_probe.preferred_size()[1], agp_probe.preferred_size()[1])
    n_cols = max(1, min(n_cols, n))
    rows_per_col = (n + n_cols - 1) // n_cols

    if available_h is not None and n > 0:
        target = (available_h - ROW_GAP * (rows_per_col - 1)) / rows_per_col
        row_h = max(min(target, preferred_row_h), 0.45)
    else:
        row_h = preferred_row_h

    s      = row_h / preferred_row_h if preferred_row_h > 0 else 1.0
    card_w = card_probe.CARD_W * s
    agp_w  = agp_probe.CARD_W  * s
    col_w  = card_w + LINE_GAP + agp_w
    max_right = start_x

    for idx, d in enumerate(saas_app_data):
        col = idx // rows_per_col
        row = idx %  rows_per_col
        cx  = start_x + col * (col_w + COL_GAP)
        cy  = start_y + row * (row_h + ROW_GAP)
        app_card = SaaSAppCard.from_dict(d)
        cloud    = (d.get('cloud') or d.get('agp_cloud') or '').lower()
        agp_cfg  = cloud_lookup.get(cloud) if cloud else None
        agp_card = SaaSAGPCard.from_config(agp_cfg) if agp_cfg else None
        missing_cloud = not cloud and bool(agp_configs)

        shapes.extend(app_card.render(cx, cy, card_w, row_h))

        if agp_card:
            agp_x = cx + card_w + LINE_GAP
            ly = (cy
                  + (card_probe.LABEL_H + card_probe.UNDERLINE_H + card_probe.LABEL_GAP) * s
                  + card_probe.INNER_PAD * s
                  + card_probe.ICON_SIZE * s / 2)
            ly2 = agp_card.line_anchor_y(cy, scale=s)
            line_y = (ly + ly2) / 2
            shapes.append(line(cx + card_w, line_y, agp_x, line_y,
                               stroke=COLORS['text_muted'], sw=1.0, dash='dash'))
            _brk  = _AGB()
            _bs   = s * 0.85
            mid_x = cx + card_w + LINE_GAP / 2
            bw, bh = _brk.BOLT_W * _bs, _brk.BOLT_H * _bs
            ws    = _brk.WALL_SIZE * _bs
            lh    = _brk.LABEL_H * _bs
            lg    = _brk.LABEL_GAP * _bs
            bg    = _brk.BOLT_GAP * _bs
            by0   = line_y - _brk.LINE_Y_FROM_TOP * _bs
            shapes.append(_img(mid_x - bw/2, by0, bw, bh, IMAGES['agp_bolt']))
            wy = by0 + bh + bg
            shapes.append(_img(mid_x - ws/2, wy, ws, ws, IMAGES['agp_firewall']))
            shapes.append(_txt(mid_x - _brk.W * _bs / 2, wy + ws + lg,
                               _brk.W * _bs, lh, '"Airgap"',
                               fs=max(5, round(7 * _bs)),
                               color=COLORS['text_primary'],
                               align='center', valign='middle'))
            shapes.extend(agp_card.render(agp_x, cy, agp_w, row_h))
            max_right = max(max_right, agp_x + agp_w)
        elif missing_cloud:
            agp_x  = cx + card_w + LINE_GAP
            warn_w = 1.2 * s
            shapes.append(line(cx + card_w, cy + row_h/2, agp_x, cy + row_h/2,
                               stroke=COLORS['negative'], sw=1.0, dash='dash'))
            shapes.append(_txt(agp_x, cy, warn_w, row_h, '⚠ cloud?',
                               fs=max(7, round(8*s)), color=COLORS['negative'],
                               align='center', valign='middle'))
            max_right = max(max_right, agp_x + warn_w)
        else:
            max_right = max(max_right, cx + card_w)

    total_h = rows_per_col * row_h + (rows_per_col - 1) * ROW_GAP
    return shapes, (start_x, start_y, max_right - start_x, total_h)


# ── Proxy for saas_app group in registry ──────────────────────────────────

class _FloatProxy:
    zone = 'float'
    agp_source = 'never'
    priority = 3

    def __init__(self, w, h):
        self._w, self._h = w, h

    def routing_anchors(self, x, y, w, h):
        return {'top_center': (x + w/2, y), 'bottom_center': (x + w/2, y + h),
                'left_center': (x, y + h/2), 'right_center': (x + w, y + h/2)}

    def copy_badge_anchor(self, x, y, w, h):
        return None


# ── get_layout_bounds (for AI reviewer queries) ────────────────────────────

def get_layout_bounds(scenario):
    """Return bounding boxes without rendering. Used by the AI reviewer."""
    sites_data  = scenario.get('sites', [])
    _all_pairs  = [(d, build_component(d)) for d in sites_data]
    row_pairs   = [(d, c) for d, c in _all_pairs if c.zone == 'main_row']
    regular_data = [d for d, _ in row_pairs]
    sites        = [c for _, c in row_pairs]

    unity_reserve = 0.0
    if scenario.get('unity', True):
        unity_reserve = UnityCard().preferred_size()[1] + 0.12

    auto_gaps = _edge_gaps(sites_data, scenario.get('connections', []))
    base_gap  = scenario.get('site_gap')
    if isinstance(base_gap, (int, float)) and base_gap > 0:
        auto_gaps = [float(base_gap)] * max(0, len(sites) - 1)

    sites_max_w = CANVAS_W - MARGIN_LEFT - MARGIN_RIGHT
    agp_list = scenario.get('agps') or ([scenario['agp']] if scenario.get('agp') else [])
    if agp_list:
        _probe    = AGPZone(agp_list[0])
        agp_min_w, _ = _probe.min_size()
        sites_max_w = max(CANVAS_W * 0.40,
                          CANVAS_W - MARGIN_LEFT - MARGIN_RIGHT - agp_min_w - AGP_GAP)

    rows = solve_rows(sites, max_w=sites_max_w, canvas_h=CANVAS_H,
                      start_x=MARGIN_LEFT,
                      start_y=MARGIN_TOP + 0.1 + unity_reserve,
                      gaps=auto_gaps)
    placements = [p for row in rows for p in row.placements]

    site_bounds = []
    rects = []
    for d, p in zip(regular_data, placements):
        cr = p.component.container_rect(p.x, p.y, p.w, p.h) if hasattr(p.component, 'container_rect') else (p.x, p.y, p.w, p.h)
        rects.append(cr)
        cx, cy, cw, ch = cr
        site_bounds.append({'id': d.get('id') or _slugify(d.get('name', '')),
                            'name': d.get('name', ''),
                            'x': round(cx, 3), 'y': round(cy, 3),
                            'w': round(cw, 3), 'h': round(ch, 3)})

    agp_bounds = None
    if agp_list and placements:
        zone     = AGPZone(agp_list[0])
        site_ids = [d.get('id') for d in regular_data]
        ax, ay, opt = _score_agp_placement(zone, rects, None, site_ids)
        zone.apply_size_option(opt)
        aw, ah = zone.preferred_size()
        agp_bounds = {'x': round(ax, 3), 'y': round(ay, 3),
                      'w': round(aw, 3), 'h': round(ah, 3)}

    return {'canvas_w': CANVAS_W, 'canvas_h': CANVAS_H,
            'sites': site_bounds, 'agp': agp_bounds}


# ── generate_layout — 10-phase orchestrator ────────────────────────────────

def generate_layout(scenario):
    """Main entry point. Zero isinstance. Zero type-string checks."""
    sites_data = scenario.get('sites', [])
    title      = scenario.get('title', f'Future State — {len(sites_data)} Sites')
    shapes     = [_title_shape(title)]
    registry   = PlacementRegistry()

    # ── Phase 0: Parse ─────────────────────────────────────────────────────
    all_comps = []
    for d in sites_data:
        comp    = build_component(d)
        comp_id = d.get('id') or _slugify(d.get('name', f'site_{len(all_comps)}'))
        all_comps.append((comp_id, comp, d))

    _agp_single = scenario.get('agp')
    _agp_list   = scenario.get('agps', [])
    agp_configs  = _agp_list if _agp_list else ([_agp_single] if _agp_single else [])
    agp_comps = [(f'__agp_{i}__', AGPZone(cfg), cfg)
                 for i, cfg in enumerate(agp_configs)]

    by_zone = defaultdict(list)
    for comp_id, comp, raw in all_comps:
        by_zone[comp.zone].append((comp_id, comp, raw))
    for comp_id, comp, cfg in agp_comps:
        by_zone[comp.zone].append((comp_id, comp, cfg))

    saas_app_data = [raw for _, comp, raw in by_zone.get('float', [])
                     if comp.layout_group == 'saas_pair' and isinstance(raw, dict)]

    # ── Phase 1: Header ────────────────────────────────────────────────────
    unity_reserve = 0.0
    header_zone   = by_zone.get('header', [])
    if scenario.get('unity', True) and not header_zone:
        uc = UnityCard()
        header_zone = [('__unity__', uc, None)]

    for comp_id, comp, _ in header_zone:
        pw, ph = comp.preferred_size()
        registry.register(comp_id, comp, MARGIN_LEFT, MARGIN_TOP - 0.05, pw, ph)
        unity_reserve = max(unity_reserve, ph + 0.12)

    row_top_y = MARGIN_TOP + 0.1 + unity_reserve

    # ── Phase 2: Panel reserve ─────────────────────────────────────────────
    agp_position = (scenario.get('agp_position') or 'right').lower()
    panel_reserve = 0.0
    if agp_position != 'bottom':
        for _, comp, _ in by_zone.get('right_panel', []):
            mw, _ = comp.min_size()
            panel_reserve = max(panel_reserve, mw + AGP_GAP)

    sites_max_w = CANVAS_W - MARGIN_LEFT - MARGIN_RIGHT
    if panel_reserve > 0:
        sites_max_w = max(CANVAS_W * 0.40,
                          CANVAS_W - MARGIN_LEFT - MARGIN_RIGHT - panel_reserve)

    # ── Phase 3: Main row packing ──────────────────────────────────────────
    row_entries = by_zone.get('main_row', [])
    row_comps   = [comp for _, comp, _ in row_entries]
    row_dicts   = [raw  for _, _, raw  in row_entries]

    base_gap  = scenario.get('site_gap')
    auto_gaps = _edge_gaps(row_dicts, scenario.get('connections', []))
    if isinstance(base_gap, (int, float)) and base_gap > 0:
        auto_gaps = [float(base_gap)] * max(0, len(row_comps) - 1)

    rows = solve_rows(row_comps, max_w=sites_max_w, canvas_h=CANVAS_H,
                      start_x=MARGIN_LEFT, start_y=row_top_y, gaps=auto_gaps)
    all_placements = [p for row in rows for p in row.placements]
    site_rects = []

    for i, (comp_id, comp, raw) in enumerate(row_entries):
        if i >= len(all_placements):
            break
        pl = all_placements[i]
        lo = (raw or {}).get('layout') or {} if isinstance(raw, dict) else {}
        px = pl.x if lo.get('x') is None else lo['x']
        py = pl.y if lo.get('y') is None else lo['y']
        shapes.extend(comp.render(px, py, pl.w, pl.h))
        registry.register(comp_id, comp, px, py, pl.w, pl.h)
        cr = comp.container_rect(px, py, pl.w, pl.h) if hasattr(comp, 'container_rect') else (px, py, pl.w, pl.h)
        site_rects.append(cr)

    # Inter-site connections
    shapes.extend(_route_connections(scenario, row_dicts, registry))

    # ── Phase 4: Panel placement + AGP source lines ────────────────────────
    row_ids = [comp_id for comp_id, _, _ in row_entries]

    replication_targets = {c['to'] for c in scenario.get('connections', [])
                           if _is_replication(c)}
    sites_with_local = [(comp_id, comp)
                        for comp_id, comp, raw in row_entries
                        if isinstance(raw, dict) and
                           raw.get('backup_target') not in (None, 'none', 'cloud')]

    if not sites_with_local:
        agp_badge_num = '1'
    elif replication_targets:
        agp_badge_num = '3'
    else:
        agp_badge_num = str(min(len(sites_with_local), 2) + 1)

    agp_position = (scenario.get('agp_position') or 'right').lower()
    panel_entries = by_zone.get('right_panel', [])

    if agp_position == 'bottom' and panel_entries:
        # Stack all AGP zones horizontally below the site row
        max_site_bottom = (max(r[1] + r[3] for r in site_rects)
                           if site_rects else row_top_y)
        bottom_y  = max_site_bottom + 0.55
        cursor_x  = MARGIN_LEFT
        for i, (comp_id, comp, cfg) in enumerate(panel_entries):
            src_ids = agp_source_ids(scenario, agp_index=i)
            agp_shps, ax, ay, aw, ah = _place_agp_zone_bottom(
                comp, cursor_x, bottom_y, src_ids, registry)
            shapes.extend(agp_shps)
            registry.register(comp_id, comp, ax, ay, aw, ah)
            t_anchors = comp.routing_anchors(ax, ay, aw, ah)
            target_y  = t_anchors['cloud_entry'][1]
            shapes.extend(_copy_badge(ax + aw + 0.05, ay, agp_badge_num))
            cursor_x = ax + aw + AGP_GAP
    else:
        for i, (comp_id, comp, cfg) in enumerate(panel_entries):
            src_ids   = agp_source_ids(scenario, agp_index=i)
            agp_shps, ax, ay, aw, ah = _place_agp_zone(
                comp, site_rects, src_ids, row_ids, registry)
            shapes.extend(agp_shps)
            registry.register(comp_id, comp, ax, ay, aw, ah)
            # AGP copy badge
            bus_x    = ax - 0.15
            t_anchors = comp.routing_anchors(ax, ay, aw, ah)
            target_y  = t_anchors['cloud_entry'][1]
            shapes.extend(_copy_badge(bus_x - 0.12, target_y - 0.12, agp_badge_num))

    # ── Phase 5: Float placement ───────────────────────────────────────────
    float_entries = by_zone.get('float', [])
    saas_app_entries = [(cid, c, r) for cid, c, r in float_entries
                        if c.layout_group == 'saas_pair']
    other_float = [(cid, c, r) for cid, c, r in float_entries
                   if c.layout_group != 'saas_pair']

    if saas_app_data:
        from placement_strategy import saas_app_placement
        spot = saas_app_placement(
            saas_app_data, None, None, None,
            site_top_y=row_top_y, margin_left=MARGIN_LEFT,
            fallback_gap=SITE_GAP,
            saas_layout=scenario.get('saas_layout'),
            registry=registry,
        )
        float_shapes, float_bbox = _place_saas_app_rows(
            saas_app_data, agp_configs,
            spot['x'], spot['y'],
            available_h=spot['available_h'],
            n_cols=spot.get('n_cols', 1),
        )
        shapes.extend(float_shapes)
        if float_bbox:
            fx, fy, fw, fh = float_bbox
            registry.register('__saas_app_group__',
                               _FloatProxy(fw, fh), fx, fy, fw, fh)

    if other_float:
        occupied    = [Rect(*r) for r in registry.all_rects()]
        canvas_rect = Rect(MARGIN_LEFT, row_top_y,
                           CANVAS_W - MARGIN_LEFT - MARGIN_RIGHT,
                           CANVAS_H - row_top_y - 0.3)
        free = free_rects(canvas_rect, occupied)
        for comp_id, comp, raw in other_float:
            pw, ph = comp.preferred_size()
            best   = find_best(free, pw, ph, prefer='top_right')
            if best is None and free:
                best = free[0]
            if best is None:
                best = canvas_rect
            shapes.extend(comp.render(best.x, best.y, pw, ph))
            registry.register(comp_id, comp, best.x, best.y, pw, ph)
            occupied.append(Rect(best.x, best.y, pw, ph))
            free = free_rects(canvas_rect, occupied)

    # ── Phase 6: Reviewer ──────────────────────────────────────────────────
    _reviewer_pass(registry, max_passes=3)

    # ── Phase 7: (connections already drawn above) ─────────────────────────

    # ── Phase 8: Copy badges ───────────────────────────────────────────────
    onprem_n = 0
    for comp_id, comp, cx, cy, cw, ch in registry.items():
        anchor = comp.copy_badge_anchor(cx, cy, cw, ch)
        if anchor is None:
            continue
        if replication_targets:
            badge_num = '2' if comp_id in replication_targets else '1'
        else:
            onprem_n += 1
            badge_num = str(onprem_n)
        shapes.extend(_copy_badge(anchor[0], anchor[1], badge_num))

    # ── Phase 9: Centering pass ────────────────────────────────────────────
    body_shapes = shapes[1:]
    body_max_x = body_min_x = MARGIN_LEFT
    for s in body_shapes:
        if s['type'] == 'line':
            body_max_x = max(body_max_x, s['x1'], s['x2'])
            body_min_x = min(body_min_x, s['x1'], s['x2'])
        else:
            body_max_x = max(body_max_x, s.get('x', 0) + s.get('w', 0))
            body_min_x = min(body_min_x, s.get('x', 0))
    body_w = body_max_x - body_min_x
    if 0 < body_w < SLIDE_W - 0.4:
        shift = (SLIDE_W - body_w) / 2 - body_min_x
        if shift > 0.05:
            for s in body_shapes:
                if s['type'] == 'line':
                    s['x1'] += shift; s['x2'] += shift
                else:
                    s['x'] = s.get('x', 0) + shift

    # Center header cards over shifted content
    content_w_so_far, _ = _content_bbox(shapes)
    for comp_id, comp, _ in header_zone:
        pw, ph = comp.preferred_size()
        ux = max(MARGIN_LEFT, (content_w_so_far - pw) / 2)
        uy = MARGIN_TOP - 0.05
        shapes = shapes[:1] + list(comp.render(ux, uy, pw, ph)) + shapes[1:]

    # ── Phase 10: Output ───────────────────────────────────────────────────
    content_w, content_h = _content_bbox(shapes)
    return {
        'background': COLORS['bg'],
        'content_w':  round(content_w + MARGIN_LEFT, 4),
        'content_h':  round(content_h + 0.3, 4),
        'shapes':     shapes,
    }
