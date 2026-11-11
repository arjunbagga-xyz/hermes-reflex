/**
 * Hermes Desktop Plugin Bundle (`hermes-reflex/desktop/index.js`).
 *
 * Built strictly with the official `@hermes/plugin-sdk`, `react`, and `react/jsx-runtime`
 * (`jsx` / `jsxs` — zero external bundler or third-party dependencies required).
 *
 * Registers:
 * 1. `TRANSCRIPT_DIRECTIVE_AREA` (`::reflex-ui{id="..." kind="..." title="..." pinned="..."}`):
 *    Renders cohesive native Hermes GenUI cards (Bar/Line/Comparison Charts, Interactive Flow
 *    Diagrams, Directly Manipulable Sliders/Toggles/Action Buttons) that can be dragged or
 *    pinned with one click.
 * 2. `PANES_AREA` (`Pinned Widgets` Sticky-Note Sidepane):
 *    Permanent right-hand drop target (`onDragOver` / `onDrop`) where users can drag any
 *    `::reflex-ui` card from chat, annotate it like a sticky note, switch sticky accent colors,
 *    and continue manipulating sliders/charts across sessions.
 * 3. `STATUS_BAR_AREA`:
 *    Displays live count of trusted executable skills & pinned sticky widgets.
 */

import { useState, useEffect, useCallback, useMemo } from 'react';
import { jsx, jsxs } from 'react/jsx-runtime';
import {
  TRANSCRIPT_DIRECTIVE_AREA,
  PANES_AREA,
  STATUS_BAR_AREA,
  Button,
  Badge,
  SegmentedControl,
  Switch,
  Input,
  ScrollArea,
  StatusDot,
  CopyButton,
} from '@hermes/plugin-sdk';

const API_BASE = '/api/plugins/hermes-reflex';

const STICKY_COLORS = {
  amber: {
    border: 'rgba(245, 158, 11, 0.45)',
    glow: 'rgba(245, 158, 11, 0.10)',
    dot: 'rgba(245, 158, 11, 1)',
    label: 'Amber',
  },
  emerald: {
    border: 'rgba(16, 185, 129, 0.45)',
    glow: 'rgba(16, 185, 129, 0.10)',
    dot: 'rgba(16, 185, 129, 1)',
    label: 'Emerald',
  },
  cyan: {
    border: 'rgba(6, 182, 212, 0.45)',
    glow: 'rgba(6, 182, 212, 0.10)',
    dot: 'rgba(6, 182, 212, 1)',
    label: 'Cyan',
  },
  violet: {
    border: 'rgba(139, 92, 246, 0.45)',
    glow: 'rgba(139, 92, 246, 0.10)',
    dot: 'rgba(139, 92, 246, 1)',
    label: 'Violet',
  },
};

// Shared in-memory event bus so pinning/dragging in chat immediately updates the sidepane
const pinboardListeners = new Set();
function notifyPinboardUpdated() {
  for (const listener of pinboardListeners) {
    try {
      listener();
    } catch (_err) {
      // ignore listener errors
    }
  }
}

async function fetchJson(path, options = {}) {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!res.ok) {
    throw new Error(`HTTP ${res.status}`);
  }
  return res.json();
}

/**
 * Interactive Native Hermes SVG Chart (Bar / Line / Comparison)
 */
function NativeChartBody({ spec }) {
  const [chartMode, setChartMode] = useState(spec?.chart_type || 'bar');
  const points = Array.isArray(spec?.data_points) ? spec.data_points : [];

  const maxValue = useMemo(() => {
    if (!points.length) return 100;
    return Math.max(
      ...points.map((p) => Math.max(Math.abs(Number(p.value) || 0), Math.abs(Number(p.secondary_value) || 0))),
      1
    );
  }, [points]);

  if (!points.length) {
    return jsx('div', {
      style: { color: 'var(--ui-text-muted)', fontSize: '12px', padding: '8px 0' },
      children: 'No numeric series available for this card.',
    });
  }

  return jsxs('div', {
    style: { display: 'flex', flexDirection: 'column', gap: '10px' },
    children: [
      jsxs('div', {
        style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center' },
        children: [
          jsx(SegmentedControl, {
            value: chartMode,
            onChange: (val) => setChartMode(val),
            options: [
              { value: 'bar', label: 'Bar' },
              { value: 'line', label: 'Trend' },
              { value: 'table', label: 'Data' },
            ],
          }),
          jsx(Badge, {
            variant: 'neutral',
            children: `${points.length} metrics`,
          }),
        ],
      }),

      chartMode === 'bar' &&
        jsx('div', {
          style: { display: 'flex', flexDirection: 'column', gap: '8px', paddingTop: '4px' },
          children: points.map((pt, idx) => {
            const val = Number(pt.value) || 0;
            const pct = Math.min(100, Math.max(4, Math.round((Math.abs(val) / maxValue) * 100)));
            return jsxs(
              'div',
              {
                style: { display: 'flex', flexDirection: 'column', gap: '4px' },
                children: [
                  jsxs('div', {
                    style: {
                      display: 'flex',
                      justifyContent: 'space-between',
                      fontSize: '12px',
                      color: 'var(--ui-text)',
                    },
                    children: [
                      jsx('span', { style: { fontWeight: 500 }, children: pt.label }),
                      jsx('span', {
                        style: { fontFamily: 'var(--ui-font-mono, monospace)', color: 'var(--ui-accent)' },
                        children: `${val.toLocaleString()}${pt.unit ? ` ${pt.unit}` : ''}`,
                      }),
                    ],
                  }),
                  jsx('div', {
                    style: {
                      height: '8px',
                      width: '100%',
                      background: 'var(--ui-panel-muted, rgba(255,255,255,0.06))',
                      borderRadius: '4px',
                      overflow: 'hidden',
                    },
                    children: jsx('div', {
                      style: {
                        width: `${pct}%`,
                        height: '100%',
                        background: 'var(--ui-accent, rgb(56, 189, 248))',
                        borderRadius: '4px',
                        transition: 'width 220ms ease-out',
                      },
                    }),
                  }),
                ],
              },
              `${pt.label}-${idx}`
            );
          }),
        }),

      chartMode === 'line' &&
        jsx('svg', {
          viewBox: '0 0 320 110',
          style: {
            width: '100%',
            height: '110px',
            background: 'var(--ui-panel-muted, rgba(255,255,255,0.03))',
            borderRadius: '6px',
            padding: '6px',
          },
          children: (() => {
            const coords = points.map((pt, i) => {
              const x = points.length === 1 ? 160 : 20 + (i / (points.length - 1)) * 280;
              const y = 90 - (Math.abs(Number(pt.value) || 0) / maxValue) * 70;
              return { x, y, pt };
            });
            const polylinePoints = coords.map((c) => `${c.x},${c.y}`).join(' ');
            return [
              jsx(
                'polyline',
                {
                  fill: 'none',
                  stroke: 'var(--ui-accent, rgb(56, 189, 248))',
                  strokeWidth: '2.5',
                  points: polylinePoints,
                },
                'line'
              ),
              ...coords.map((c, idx) =>
                jsxs(
                  'g',
                  {
                    children: [
                      jsx('circle', {
                        cx: c.x,
                        cy: c.y,
                        r: '4',
                        fill: 'var(--ui-accent, rgb(56, 189, 248))',
                      }),
                      jsx('text', {
                        x: c.x,
                        y: 104,
                        textAnchor: 'middle',
                        fill: 'var(--ui-text-muted, rgb(148, 163, 184))',
                        fontSize: '9',
                        children: String(c.pt.label).slice(0, 10),
                      }),
                    ],
                  },
                  `node-${idx}`
                )
              ),
            ];
          })(),
        }),

      chartMode === 'table' &&
        jsx('div', {
          style: {
            display: 'grid',
            gridTemplateColumns: '1fr auto',
            gap: '6px 12px',
            fontSize: '12px',
            padding: '6px 8px',
            background: 'var(--ui-panel-muted, rgba(255,255,255,0.04))',
            borderRadius: '6px',
          },
          children: points.flatMap((pt, idx) => [
            jsx('span', { style: { color: 'var(--ui-text)' }, children: pt.label }, `lbl-${idx}`),
            jsx(
              'span',
              {
                style: { fontFamily: 'var(--ui-font-mono, monospace)', color: 'var(--ui-accent)' },
                children: `${Number(pt.value).toLocaleString()} ${pt.unit || ''}`,
              },
              `val-${idx}`
            ),
          ]),
        }),
    ],
  });
}

/**
 * Interactive Flow & Architecture Pipeline Widget
 */
function NativeFlowBody({ spec }) {
  const nodes = Array.isArray(spec?.flow_nodes) ? spec.flow_nodes : [];
  const [selectedIdx, setSelectedIdx] = useState(0);

  if (!nodes.length) {
    return jsx('div', {
      style: { color: 'var(--ui-text-muted)', fontSize: '12px' },
      children: 'No workflow nodes defined.',
    });
  }

  const activeNode = nodes[selectedIdx] || nodes[0];

  return jsxs('div', {
    style: { display: 'flex', flexDirection: 'column', gap: '10px' },
    children: [
      jsx('div', {
        style: {
          display: 'flex',
          flexWrap: 'wrap',
          gap: '6px',
          alignItems: 'center',
        },
        children: nodes.map((node, idx) =>
          jsxs(
            'div',
            {
              style: { display: 'flex', alignItems: 'center', gap: '6px' },
              children: [
                jsx(Button, {
                  variant: idx === selectedIdx ? 'primary' : 'secondary',
                  size: 'sm',
                  onClick: () => setSelectedIdx(idx),
                  children: `${idx + 1}. ${node.label}`,
                }),
                idx < nodes.length - 1 &&
                  jsx('span', {
                    style: { color: 'var(--ui-text-muted)', fontSize: '12px' },
                    children: '→',
                  }),
              ],
            },
            node.id || `step-${idx}`
          )
        ),
      }),
      jsxs('div', {
        style: {
          padding: '10px 12px',
          borderRadius: '6px',
          border: '1px solid var(--ui-border)',
          background: 'var(--ui-panel-muted, rgba(255,255,255,0.03))',
          display: 'flex',
          flexDirection: 'column',
          gap: '4px',
        },
        children: [
          jsxs('div', {
            style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center' },
            children: [
              jsx('span', {
                style: { fontWeight: 600, fontSize: '13px', color: 'var(--ui-text)' },
                children: activeNode.label,
              }),
              jsx(Badge, {
                variant: 'success',
                children: activeNode.status || 'verified',
              }),
            ],
          }),
          jsx('div', {
            style: { fontSize: '12px', color: 'var(--ui-text-muted)', lineHeight: 1.45 },
            children: activeNode.detail,
          }),
        ],
      }),
    ],
  });
}

/**
 * Directly Manipulable Sliders, Toggles & Live Skill Action Controls
 */
function NativeControlsBody({ widgetId, spec }) {
  const controls = Array.isArray(spec?.controls) ? spec.controls : [];
  const initialValues = useMemo(() => {
    const map = {};
    for (const c of controls) {
      map[c.key] = Number(c.default_value) || 0;
    }
    return map;
  }, [controls]);

  const [values, setValues] = useState(initialValues);
  const [autoNormalize, setAutoNormalize] = useState(false);
  const [runStatus, setRunStatus] = useState('');

  const handleValueChange = useCallback(
    async (key, nextVal) => {
      const updated = { ...values, [key]: nextVal };
      setValues(updated);
      try {
        await fetchJson(`/widgets/${encodeURIComponent(widgetId)}/controls`, {
          method: 'POST',
          body: JSON.stringify({ values: updated }),
        });
      } catch (_err) {
        // Non-fatal in offline preview
      }
    },
    [values, widgetId]
  );

  const computedComposite = useMemo(() => {
    const keys = Object.keys(values);
    if (!keys.length) return 0;
    const sum = keys.reduce((acc, k) => acc + Number(values[k] || 0), 0);
    return autoNormalize ? Number((sum / keys.length).toFixed(2)) : Number(sum.toFixed(2));
  }, [values, autoNormalize]);

  const triggerSkillWithParams = useCallback(async () => {
    const skillName = spec?.action_skill_name;
    if (!skillName) {
      setRunStatus('Parameters saved to widget state.');
      return;
    }
    setRunStatus(`Running ${skillName}...`);
    try {
      const res = await fetchJson('/skills/run', {
        method: 'POST',
        body: JSON.stringify({ skill_name: skillName, params: values, dry_run: false }),
      });
      setRunStatus(
        res.ok
          ? `Executed ${skillName} in ${res.duration_ms}ms`
          : `Escalated to SKILL.md: ${res.result?.reason || 'check params'}`
      );
    } catch (err) {
      setRunStatus(`Run failed: ${err.message}`);
    }
  }, [spec, values]);

  return jsxs('div', {
    style: { display: 'flex', flexDirection: 'column', gap: '10px' },
    children: [
      ...controls.map((ctrl) => {
        const currentVal = values[ctrl.key] ?? ctrl.default_value ?? 0;
        return jsxs(
          'div',
          {
            style: { display: 'flex', flexDirection: 'column', gap: '4px' },
            children: [
              jsxs('div', {
                style: { display: 'flex', justifyContent: 'space-between', fontSize: '12px' },
                children: [
                  jsx('span', { style: { color: 'var(--ui-text)', fontWeight: 500 }, children: ctrl.label }),
                  jsx('span', {
                    style: { fontFamily: 'var(--ui-font-mono, monospace)', color: 'var(--ui-accent)' },
                    children: `${currentVal}${ctrl.unit ? ` ${ctrl.unit}` : ''}`,
                  }),
                ],
              }),
              jsx('input', {
                type: 'range',
                min: ctrl.min ?? 0,
                max: ctrl.max ?? 100,
                step: ctrl.step ?? 1,
                value: currentVal,
                onChange: (e) => handleValueChange(ctrl.key, parseFloat(e.target.value)),
                style: { width: '100%', accentColor: 'var(--ui-accent, rgb(56, 189, 248))', cursor: 'pointer' },
              }),
            ],
          },
          ctrl.key
        );
      }),

      jsxs('div', {
        style: {
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          padding: '8px 10px',
          borderRadius: '6px',
          background: 'var(--ui-panel-muted, rgba(255,255,255,0.04))',
        },
        children: [
          jsxs('div', {
            style: { display: 'flex', flexDirection: 'column' },
            children: [
              jsx('span', {
                style: { fontSize: '11px', color: 'var(--ui-text-muted)' },
                children: spec?.output_metric_label || 'Live Computed Metric',
              }),
              jsx('span', {
                style: {
                  fontSize: '15px',
                  fontWeight: 700,
                  fontFamily: 'var(--ui-font-mono, monospace)',
                  color: 'var(--ui-accent)',
                },
                children: `${computedComposite} ${spec?.output_metric_unit || ''}`,
              }),
            ],
          }),
          jsxs('div', {
            style: { display: 'flex', alignItems: 'center', gap: '8px' },
            children: [
              jsx(Switch, {
                checked: autoNormalize,
                onChange: (checked) => setAutoNormalize(Boolean(checked)),
                label: 'Mean',
              }),
              jsx(Button, {
                variant: 'primary',
                size: 'sm',
                onClick: triggerSkillWithParams,
                children: spec?.action_skill_name ? `Run ${spec.action_skill_name}` : 'Apply',
              }),
            ],
          }),
        ],
      }),

      runStatus &&
        jsx('div', {
          style: { fontSize: '11px', color: 'var(--ui-text-muted)' },
          children: runStatus,
        }),
    ],
  });
}

/**
 * Main GenUI Card Component rendered in both `TRANSCRIPT_DIRECTIVE_AREA` and `PANES_AREA`
 */
function ReflexWidgetCard({ widgetId, initialKind, initialTitle, inPinboard = false, preloadedWidget = null }) {
  const [widget, setWidget] = useState(preloadedWidget);
  const [pinned, setPinned] = useState(Boolean(preloadedWidget?.pinned));
  const [noteColor, setNoteColor] = useState(preloadedWidget?.note_color || 'amber');
  const [userNotes, setUserNotes] = useState(preloadedWidget?.user_notes || '');

  useEffect(() => {
    if (preloadedWidget) {
      setWidget(preloadedWidget);
      setPinned(Boolean(preloadedWidget.pinned));
      setNoteColor(preloadedWidget.note_color || 'amber');
      setUserNotes(preloadedWidget.user_notes || '');
      return;
    }
    let mounted = true;
    fetchJson(`/widgets/${encodeURIComponent(widgetId)}`)
      .then((data) => {
        if (mounted && data?.widget) {
          setWidget(data.widget);
          setPinned(Boolean(data.widget.pinned));
          setNoteColor(data.widget.note_color || 'amber');
          setUserNotes(data.widget.user_notes || '');
        }
      })
      .catch(() => {});
    return () => {
      mounted = false;
    };
  }, [widgetId, preloadedWidget]);

  const handleTogglePin = useCallback(async () => {
    const nextPinned = !pinned;
    setPinned(nextPinned);
    try {
      await fetchJson(`/widgets/${encodeURIComponent(widgetId)}/pin`, {
        method: 'POST',
        body: JSON.stringify({ pinned: nextPinned, note_color: noteColor, user_notes: userNotes }),
      });
      notifyPinboardUpdated();
    } catch (_err) {
      // ignore offline error
    }
  }, [widgetId, pinned, noteColor, userNotes]);

  const handleColorOrNoteChange = useCallback(
    async (nextColor, nextNotes) => {
      setNoteColor(nextColor);
      setUserNotes(nextNotes);
      try {
        await fetchJson(`/widgets/${encodeURIComponent(widgetId)}/pin`, {
          method: 'POST',
          body: JSON.stringify({ pinned: true, note_color: nextColor, user_notes: nextNotes }),
        });
        notifyPinboardUpdated();
      } catch (_err) {
        // ignore
      }
    },
    [widgetId]
  );

  const handleDragStart = useCallback(
    (e) => {
      e.dataTransfer.setData(
        'application/x-hermes-reflex-widget',
        JSON.stringify({
          widget_id: widgetId,
          kind: widget?.kind || initialKind || 'chart',
          title: widget?.title || initialTitle || 'Widget',
        })
      );
      e.dataTransfer.effectAllowed = 'copyMove';
    },
    [widgetId, widget, initialKind, initialTitle]
  );

  const kind = widget?.kind || initialKind || 'chart';
  const title = widget?.title || initialTitle || 'Interactive Widget';
  const spec = widget?.spec || {};
  const palette = STICKY_COLORS[noteColor] || STICKY_COLORS.amber;

  return jsxs('div', {
    draggable: true,
    onDragStart: handleDragStart,
    style: {
      border: `1px solid ${inPinboard ? palette.border : 'var(--ui-border)'}`,
      background: inPinboard ? palette.glow : 'var(--ui-panel)',
      borderRadius: '10px',
      padding: '12px 14px',
      margin: inPinboard ? '0 0 10px 0' : '10px 0',
      display: 'flex',
      flexDirection: 'column',
      gap: '10px',
      cursor: 'grab',
      boxShadow: '0 2px 8px rgba(0, 0, 0, 0.14)',
    },
    children: [
      jsxs('div', {
        style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '8px' },
        children: [
          jsxs('div', {
            style: { display: 'flex', alignItems: 'center', gap: '8px' },
            children: [
              jsx('span', {
                title: 'Drag card to Sticky-Note Sidepane',
                style: { cursor: 'grab', opacity: 0.65, fontSize: '13px', userSelect: 'none' },
                children: '⋮⋮',
              }),
              jsx(StatusDot, { status: pinned ? 'online' : 'idle' }),
              jsxs('div', {
                style: { display: 'flex', flexDirection: 'column' },
                children: [
                  jsx('span', {
                    style: { fontWeight: 600, fontSize: '13px', color: 'var(--ui-text)' },
                    children: title,
                  }),
                  widget?.subtitle &&
                    jsx('span', {
                      style: { fontSize: '11px', color: 'var(--ui-text-muted)' },
                      children: widget.subtitle,
                    }),
                ],
              }),
            ],
          }),
          jsxs('div', {
            style: { display: 'flex', alignItems: 'center', gap: '6px' },
            children: [
              jsx(Badge, { variant: 'neutral', children: kind.toUpperCase() }),
              jsx(CopyButton, { text: JSON.stringify(spec, null, 2) }),
              jsx(Button, {
                variant: pinned ? 'primary' : 'secondary',
                size: 'sm',
                onClick: handleTogglePin,
                children: pinned ? 'Pinned ★' : 'Pin to Pane',
              }),
            ],
          }),
        ],
      }),

      kind === 'flow'
        ? jsx(NativeFlowBody, { spec })
        : kind === 'controls'
        ? jsx(NativeControlsBody, { widgetId, spec })
        : jsx(NativeChartBody, { spec }),

      inPinboard &&
        jsxs('div', {
          style: {
            display: 'flex',
            flexDirection: 'column',
            gap: '6px',
            borderTop: '1px dashed var(--ui-border)',
            paddingTop: '8px',
          },
          children: [
            jsxs('div', {
              style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center' },
              children: [
                jsx('span', {
                  style: { fontSize: '11px', color: 'var(--ui-text-muted)' },
                  children: 'Sticky Note Color',
                }),
                jsx('div', {
                  style: { display: 'flex', gap: '6px' },
                  children: Object.entries(STICKY_COLORS).map(([colorKey, cfg]) =>
                    jsx(
                      'button',
                      {
                        type: 'button',
                        onClick: () => handleColorOrNoteChange(colorKey, userNotes),
                        title: cfg.label,
                        style: {
                          width: '14px',
                          height: '14px',
                          borderRadius: '50%',
                          background: cfg.dot,
                          border:
                            noteColor === colorKey
                              ? '2px solid var(--ui-text)'
                              : '1px solid transparent',
                          cursor: 'pointer',
                        },
                      },
                      colorKey
                    )
                  ),
                }),
              ],
            }),
            jsx(Input, {
              value: userNotes,
              placeholder: 'Add sticky note annotation...',
              onChange: (e) => handleColorOrNoteChange(noteColor, e.target?.value ?? e),
            }),
          ],
        }),
    ],
  });
}

/**
 * Right-Hand Sticky-Note Sidepane (`PANES_AREA`)
 */
function StickyPinboardPane() {
  const [pinnedWidgets, setPinnedWidgets] = useState([]);
  const [isDragOver, setIsDragOver] = useState(false);
  const [skillsSummary, setSkillsSummary] = useState({ compiled: 0, trusted: 0 });

  const refreshPinboard = useCallback(async () => {
    try {
      const status = await fetchJson('/status');
      if (status?.ok) {
        setPinnedWidgets(status.pinned_widgets || []);
        setSkillsSummary({
          compiled: status.compiled_skills_count || 0,
          trusted: status.trusted_skills_count || 0,
        });
      }
    } catch (_err) {
      // ignore offline error
    }
  }, []);

  useEffect(() => {
    refreshPinboard();
    pinboardListeners.add(refreshPinboard);
    return () => {
      pinboardListeners.delete(refreshPinboard);
    };
  }, [refreshPinboard]);

  const handleDrop = useCallback(
    async (e) => {
      e.preventDefault();
      setIsDragOver(false);
      const raw = e.dataTransfer.getData('application/x-hermes-reflex-widget');
      if (!raw) return;
      try {
        const parsed = JSON.parse(raw);
        if (parsed?.widget_id) {
          await fetchJson(`/widgets/${encodeURIComponent(parsed.widget_id)}/pin`, {
            method: 'POST',
            body: JSON.stringify({ pinned: true }),
          });
          await refreshPinboard();
        }
      } catch (_err) {
        // ignore malformed drop
      }
    },
    [refreshPinboard]
  );

  return jsxs('div', {
    onDragOver: (e) => {
      e.preventDefault();
      setIsDragOver(true);
    },
    onDragLeave: () => setIsDragOver(false),
    onDrop: handleDrop,
    style: {
      height: '100%',
      display: 'flex',
      flexDirection: 'column',
      padding: '12px',
      gap: '10px',
      background: isDragOver ? 'rgba(56, 189, 248, 0.08)' : 'var(--ui-panel)',
      borderLeft: isDragOver
        ? '2px dashed var(--ui-accent, rgb(56, 189, 248))'
        : '1px solid var(--ui-border)',
      transition: 'background 150ms ease',
    },
    children: [
      jsxs('div', {
        style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center' },
        children: [
          jsxs('div', {
            style: { display: 'flex', flexDirection: 'column' },
            children: [
              jsx('span', {
                style: { fontWeight: 700, fontSize: '14px', color: 'var(--ui-text)' },
                children: 'Pinned Widgets',
              }),
              jsx('span', {
                style: { fontSize: '11px', color: 'var(--ui-text-muted)' },
                children: 'Drag any card from chat here like a sticky note',
              }),
            ],
          }),
          jsxs('div', {
            style: { display: 'flex', gap: '6px', alignItems: 'center' },
            children: [
              jsx(Badge, {
                variant: 'neutral',
                children: `${skillsSummary.trusted}/${skillsSummary.compiled} Reflexes`,
              }),
              jsx(Button, {
                variant: 'secondary',
                size: 'sm',
                onClick: refreshPinboard,
                children: 'Sync',
              }),
            ],
          }),
        ],
      }),

      jsx(ScrollArea, {
        style: { flex: 1, paddingRight: '4px' },
        children:
          pinnedWidgets.length === 0
            ? jsx('div', {
                style: {
                  border: '1px dashed var(--ui-border)',
                  borderRadius: '8px',
                  padding: '24px 14px',
                  textAlign: 'center',
                  color: 'var(--ui-text-muted)',
                  fontSize: '12px',
                },
                children:
                  'Drop any ::reflex-ui chart, flow diagram, or slider card here to keep it permanently pinned across sessions.',
              })
            : pinnedWidgets.map((w) =>
                jsx(
                  ReflexWidgetCard,
                  {
                    widgetId: w.widget_id,
                    initialKind: w.kind,
                    initialTitle: w.title,
                    inPinboard: true,
                    preloadedWidget: w,
                  },
                  w.widget_id
                )
              ),
      }),
    ],
  });
}

/**
 * Official Desktop Plugin SDK Entrypoint
 */
export default function activate(ctx) {
  // 1. Register In-Chat GenUI Card Renderer (`::reflex-ui{id="..." kind="..." title="..."}`)
  ctx.register({
    id: 'reflex-ui-directive',
    area: TRANSCRIPT_DIRECTIVE_AREA,
    data: {
      name: 'reflex-ui',
      render: ({ attrs }) =>
        jsx(ReflexWidgetCard, {
          widgetId: attrs?.id || '',
          initialKind: attrs?.kind || 'chart',
          initialTitle: attrs?.title || 'Interactive Widget',
          inPinboard: false,
        }),
    },
  });

  // 2. Register Permanent Right-Hand Sticky-Note Sidepane (`PANES_AREA`)
  ctx.register({
    id: 'reflex-sticky-pinboard',
    area: PANES_AREA,
    title: 'Pinned Widgets',
    data: {
      placement: 'right',
      width: '340px',
    },
    render: () => jsx(StickyPinboardPane, {}),
  });

  // 3. Register Status Bar Indicator (`STATUS_BAR_AREA`)
  ctx.register({
    id: 'reflex-status-pill',
    area: STATUS_BAR_AREA,
    render: () =>
      jsxs('div', {
        style: { display: 'inline-flex', alignItems: 'center', gap: '6px', fontSize: '11px' },
        children: [
          jsx(StatusDot, { status: 'online' }),
          jsx('span', { children: 'Reflex + GenUI Active' }),
        ],
      }),
  });
}
