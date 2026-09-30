// The only place a view becomes chart-library settings. Swapping ApexCharts for
// another library means rewriting this folder and nothing else (DESIGN §5).
import type { ApexOptions } from 'ng-apexcharts';

import type { AnswerRow, BarView, LineView } from '../../api/types';

type UnitKind = AnswerRow['unit_kind'];

/** Axis ticks, the one formatting the client does: every figure's text is the Presenter's. */
export function tick(kind: UnitKind, value: number, unit = 'USD'): string {
  switch (kind) {
    case 'ratio':
      return `${round(value * 100)}%`;
    case 'multiple':
      return `${round(value, 2)}×`;
    case 'per_share':
      return `${symbol(unit)}${round(value, 2)}`;
    case 'money':
      return `${value < 0 ? '-' : ''}${symbol(unit)}${scaled(Math.abs(value))}`;
    case 'count':
      return scaled(value);
  }
}

function symbol(unit: string): string {
  return unit.startsWith('EUR') ? '€' : '$';
}

function scaled(value: number): string {
  for (const [size, suffix] of [
    [1e12, 'T'],
    [1e9, 'B'],
    [1e6, 'M'],
    [1e3, 'K'],
  ] as const) {
    if (Math.abs(value) >= size) return `${round(value / size)}${suffix}`;
  }
  return `${round(value)}`;
}

function round(value: number, places = 1): string {
  return String(Number(value.toFixed(places))); // 12.0 -> "12", 12.5 -> "12.5"
}

/** A row's value as the chart plots it; `null` (a growth from a zero base) is a gap. */
function plotted(row: AnswerRow): number | null {
  return row.value === null ? null : Number(row.value);
}

/** Text for a tooltip. Everything is escaped: these strings are data, not markup. */
function escape(text: string): string {
  return text.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
}

export function tooltipHtml(name: string, row: AnswerRow): string {
  const span = row.period_start
    ? `${row.period_start} → ${row.period_end}`
    : `as of ${row.period_end}`;
  const lines = [
    `<b>${escape(name)}</b>`,
    escape(row.display),
    escape(row.compared_with ? `${row.period_label} vs ${row.compared_with}` : row.period_label),
    `<span class="vf-tooltip-span">${escape(span)}</span>`,
  ];
  return `<div class="vf-tooltip">${lines.join('<br>')}</div>`;
}

// A point or bar, carrying its row's index so the tooltip reads the Presenter's own strings.
interface Datum {
  x: number | string;
  y: number | null;
  row: number;
}

interface Plotted {
  name: string;
  data: Datum[];
}

const COMMON: ApexOptions = {
  chart: {
    height: 320,
    toolbar: { show: false },
    zoom: { enabled: false },
    animations: { enabled: false },
    fontFamily: 'Roboto, sans-serif',
    background: 'transparent',
    foreColor: '#38366a', // axis and legend text, readable on the light surface
  },
  dataLabels: { enabled: false }, // the table carries the exact figures
  legend: { position: 'top' },
};

export function chartOptions(view: LineView | BarView, rows: readonly AnswerRow[]): ApexOptions {
  const series: Plotted[] =
    view.kind === 'line'
      ? view.series.map((line) => ({
          name: line.label,
          // Real dates on the x axis, so filers' different fiscal calendars show (DESIGN §5).
          data: line.rows.map((i) => ({
            x: Date.parse(rows[i].period_end),
            y: plotted(rows[i]),
            row: i,
          })),
        }))
      : [
          {
            name: view.title,
            // In the Presenter's order: it sorted a ranking; AnswerView refuses one out of order.
            data: view.rows.map((i) => ({ x: rows[i].company, y: plotted(rows[i]), row: i })),
          },
        ];

  const unit = rows[series[0].data[0].row].unit;
  const yaxis = { labels: { formatter: (value: number) => tick(view.unit_kind, value, unit) } };
  // Read back from `series` above, not from the chart's own config: two indices are all it needs.
  const custom = (at: { seriesIndex: number; dataPointIndex: number }) => {
    const line = series[at.seriesIndex];
    const row = rows[line.data[at.dataPointIndex].row];
    return tooltipHtml(view.kind === 'line' ? line.name : row.company, row);
  };

  if (view.kind === 'line') {
    return {
      ...COMMON,
      chart: { ...COMMON.chart, type: 'line' },
      xaxis: { type: 'datetime' },
      yaxis,
      series,
      stroke: { width: 2 },
      markers: { size: 4 },
      tooltip: { custom },
    };
  }
  return {
    ...COMMON,
    chart: { ...COMMON.chart, type: 'bar' },
    xaxis: { type: 'category' },
    yaxis,
    legend: { show: false },
    series,
    tooltip: { custom },
  };
}
