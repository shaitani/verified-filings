import type { BarView, LineView } from '../../api/types';
import { ANSWERS } from '../answer.testing';
import { chartOptions, tick, tooltipHtml } from './chart-options';

interface Plotted {
  name: string;
  data: { x: number | string; y: number | null; row: number }[];
}

describe('tick', () => {
  it.each([
    ['money', 391_035_000_000, '$391B'],
    ['money', -32_130_000_000, '-$32.1B'],
    ['ratio', 0.462063, '46.2%'],
    ['multiple', 0.8673, '0.87×'],
    ['per_share', 6.08, '$6.08'],
    ['count', 15_120_000_000, '15.1B'],
  ] as const)('%s %d reads %s', (kind, value, text) => {
    expect(tick(kind, value)).toBe(text);
  });
});

describe('chartOptions', () => {
  it('draws a line per series, on real dates, in the order the Presenter gave', () => {
    const answer = ANSWERS.q041; // revenue and net income, one panel
    const view = answer.views[0] as LineView;
    const series = chartOptions(view, answer.rows).series as Plotted[];

    expect(series.map((s) => s.name)).toEqual(view.series.map((s) => s.label));
    for (const [i, line] of series.entries()) {
      expect(line.data.map((d) => d.row)).toEqual(view.series[i].rows);
      expect(line.data.map((d) => d.x)).toEqual(
        view.series[i].rows.map((row) => Date.parse(answer.rows[row].period_end)),
      );
    }
  });

  it('draws bars in the Presenter order, which AnswerView guarantees is ranked', () => {
    const answer = ANSWERS.q009; // highest operating margin first
    const view = answer.views[0] as BarView;
    const [bars] = chartOptions(view, answer.rows).series as Plotted[];
    expect(bars.data.map((d) => d.x)).toEqual(view.rows.map((row) => answer.rows[row].company));
    const heights = bars.data.map((d) => d.y as number);
    expect(heights).toEqual([...heights].sort((a, b) => b - a));
  });

  it("shows the Presenter's own strings in the tooltip", () => {
    const answer = ANSWERS.q009;
    const view = answer.views[0] as BarView;
    const custom = chartOptions(view, answer.rows).tooltip?.custom as (at: {
      seriesIndex: number;
      dataPointIndex: number;
    }) => string;
    const first = answer.rows[view.rows[0]];
    const html = custom({ seriesIndex: 0, dataPointIndex: 0 });
    expect(html).toContain(first.display);
    expect(html).toContain(first.company);
  });

  it('escapes tooltip text: data is never markup', () => {
    const row = { ...ANSWERS.q001.rows[0], display: '<img src=x onerror=alert(1)>' };
    const html = tooltipHtml('<b>AAPL</b>', row);
    expect(html).not.toContain('<img');
    expect(html).toContain('&#60;img');
  });
});
