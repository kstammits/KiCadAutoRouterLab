"""Shove (split for editor search). Canonical code in shove_evict/shove_reconnect."""
from __future__ import annotations
from .shove_evict import ShoveReport, _Middle, Eviction, pin_sites, boxes_for_net, _seg_hits_box, _split_pieces, _node_xy, _node_net, _seg_node, evict_gnd_in_boxes, gnd_pad_components, new_copper_nodes, _clearance_of, _new_copper_clearance, _restore_cleared, victim_pieces_intact
from .shove_reconnect import _on_box_edge, _cut_points_for, victim_net_components, _isolated_victim_pads, _snap_emit_ends, _fly_one_gap, _reconnect_victim_gaps, _reconnect_gnd_gap, edge_clearance_ok, signal_blockers_for_net, retry_failed_with_shove, retry_failed_with_signal_shove
DEFAULT_EDGE_CLEARANCE_MM = 0.5
MIN_SPLIT_MM = 0.05
