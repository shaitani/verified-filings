import { Component, input } from '@angular/core';
import { TestBed } from '@angular/core/testing';

import type { AnswerRow, AnswerView, BarView, LineView } from '../api/types';
import { AnswerPanel } from './answer-panel';
import { ANSWERS } from './answer.testing';
import { AnswerChart } from './chart/answer-chart';
import { formula } from './citations-list';

// The chart itself is ApexCharts drawing SVG, which jsdom cannot lay out; its settings are
// tested in chart-options.spec.ts. Here it is a marker that says which view it was given.
@Component({ selector: 'vf-answer-chart', template: '{{ view().kind }}: {{ view().title }}' })
class ChartMarker {
  readonly view = input.required<LineView | BarView>();
  readonly rows = input.required<readonly AnswerRow[]>();
}

describe('AnswerPanel', () => {
  async function shown(answer: AnswerView) {
    TestBed.configureTestingModule({ imports: [AnswerPanel] });
    TestBed.overrideComponent(AnswerPanel, {
      remove: { imports: [AnswerChart] },
      add: { imports: [ChartMarker] },
    });
    const fixture = TestBed.createComponent(AnswerPanel);
    fixture.componentRef.setInput('answer', answer);
    await fixture.whenStable();
    const page = fixture.nativeElement as HTMLElement;
    return { page, text: () => (page.textContent ?? '').replace(/\s+/g, ' ') };
  }

  it('shows one figure as a stat card, and still ships the table', async () => {
    const { page } = await shown(ANSWERS.q001);
    expect(page.querySelector('vf-stat-card')?.textContent).toContain('$391.04B');
    expect(page.querySelectorAll('tr[mat-row]')).toHaveLength(1);
  });

  it('dates a balance rather than giving it a span', async () => {
    const { page } = await shown(ANSWERS.q002);
    expect(page.querySelector('vf-stat-card')?.textContent).toContain('as of 2025-06-30');
  });

  it("draws each of the Presenter's views, then every row in the table", async () => {
    const answer = ANSWERS.q013; // a series and its growth: two panels
    const { page } = await shown(answer);
    const charts = [...page.querySelectorAll('vf-answer-chart')].map((c) => c.textContent);
    expect(charts).toEqual(answer.views.map((v) => `${v.kind}: ${v.title}`));
    expect(page.querySelectorAll('tr[mat-row]')).toHaveLength(answer.rows.length);
  });

  it('puts the notes above the views, verbatim', async () => {
    const answer = ANSWERS.q009;
    const { page } = await shown(answer);
    const notes = [...page.querySelectorAll('.note .message')].map((n) => n.textContent?.trim());
    expect(notes).toEqual(answer.notes.map((n) => n.message));
    const [firstNote, firstChart] = [page.querySelector('.note')!, page.querySelector('.views')!];
    expect(firstNote.compareDocumentPosition(firstChart) & Node.DOCUMENT_POSITION_FOLLOWING).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
  });

  it('states the filter a list was cut by', async () => {
    const { text } = await shown(ANSWERS.q039);
    expect(text()).toContain(`Only where: ${ANSWERS.q039.conditions[0]}`);
  });

  it('shows an average across companies as one figure', async () => {
    const answer = ANSWERS.q040;
    const { page } = await shown(answer);
    expect(page.querySelectorAll('tr[mat-row]')).toHaveLength(1);
    expect(page.querySelector('vf-stat-card')?.textContent).toContain(answer.rows[0].companies[0]);
  });

  it('shows a change against what it is measured from', async () => {
    const { page } = await shown(ANSWERS.q020);
    expect(page.querySelector('.vs')?.textContent).toContain(ANSWERS.q020.rows[0].compared_with);
  });

  it('shows a multiple as a multiple', async () => {
    const { page } = await shown(ANSWERS.q022);
    expect(page.querySelector('vf-stat-card')?.textContent).toContain('×');
  });

  it('numbers every source the table cites, and says how each was chosen', async () => {
    const answer = ANSWERS.q006;
    const { page } = await shown(answer);
    const sources = page.querySelectorAll('.sources li');
    expect(sources).toHaveLength(Object.keys(answer.citations).length);
    expect(sources[0].querySelector('.how')?.textContent?.trim()).toBe('curated');
    expect(page.querySelector('td sup')?.textContent).toBe('[1]');
  });

  it('marks a similarity match as unreviewed', async () => {
    const answer = ANSWERS.q001;
    const [key] = Object.keys(answer.citations);
    const embedded = {
      ...answer,
      citations: { [key]: { ...answer.citations[key], resolved_by: 'embedding' as const } },
    };
    const { page } = await shown(embedded);
    expect(page.querySelector('.how.unreviewed')?.textContent?.trim()).toBe('unreviewed match');
  });
});

describe('formula', () => {
  it('names the operands of a computed figure', () => {
    const margin = Object.values(ANSWERS.q022.citations)[0]; // current ratio: c0 / c1
    const [assets, liabilities] = margin.concepts.map((c) => c.label ?? c.name);
    expect(formula(margin)).toBe(`${assets} ÷ ${liabilities}`);
  });

  it('says nothing for a figure as filed', () => {
    expect(formula(Object.values(ANSWERS.q001.citations)[0])).toBe('');
  });
});
