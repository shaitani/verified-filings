import { Component, computed, input } from '@angular/core';
import { MatExpansionModule } from '@angular/material/expansion';

import type { AnswerRow, CitationView } from '../api/types';

// How each binding was chosen, in the reader's words. "embedding" is a similarity
// match nobody reviewed, and says so (DESIGN §6).
const RESOLVED_BY: Readonly<Record<CitationView['resolved_by'], string>> = {
  alias: 'curated',
  pinned: 'your pick',
  embedding: 'unreviewed match',
};

interface Source {
  number: number;
  citation: CitationView;
  formula: string; // "Gross Profit ÷ Revenues" for arithmetic; empty for a filed figure
  companies: string; // who this source answered for
}

/**
 * What the answer was answered *as*: per source, the concept, any arithmetic over it, and
 * how it was chosen (DESIGN §6). Numbered in the order the table first cites them.
 */
@Component({
  selector: 'vf-citations-list',
  imports: [MatExpansionModule],
  template: `
    <mat-expansion-panel [expanded]="sources().length <= 3">
      <mat-expansion-panel-header>
        <mat-panel-title>Answered as</mat-panel-title>
        <mat-panel-description>{{ sources().length }} source(s)</mat-panel-description>
      </mat-expansion-panel-header>
      <ol class="sources">
        @for (source of sources(); track source.citation.key) {
          <li [value]="source.number">
            @for (concept of source.citation.concepts; track concept.concept_id) {
              <span class="concept">{{ concept.label ?? concept.name }}</span>
              <code>{{ concept.taxonomy }}:{{ concept.name }}</code>
            }
            @if (source.formula) {
              <span class="formula">= {{ source.formula }}</span>
            }
            <span class="how" [class.unreviewed]="source.citation.resolved_by === 'embedding'">
              {{ how(source.citation) }}
            </span>
            <span class="for">for {{ source.companies }}</span>
            @for (note of source.citation.notes; track $index) {
              <p class="note">{{ note.message }}</p>
            }
          </li>
        }
      </ol>
    </mat-expansion-panel>
  `,
  styleUrl: './citations-list.scss',
})
export class CitationsList {
  readonly citations = input.required<Readonly<Record<string, CitationView>>>();
  readonly rows = input.required<readonly AnswerRow[]>();
  readonly footnotes = input.required<ReadonlyMap<string, number>>();

  protected readonly sources = computed<Source[]>(() =>
    [...this.footnotes()].map(([key, number]) => {
      const citation = this.citations()[key];
      const companies = new Set(
        this.rows()
          .filter((row) => row.citations.includes(key))
          .map((row) => row.company),
      );
      return { number, citation, formula: formula(citation), companies: [...companies].join(', ') };
    }),
  );

  protected how(citation: CitationView): string {
    return RESOLVED_BY[citation.resolved_by];
  }
}

/** The arithmetic over the operands, in their names: "c0 / c1" -> "Gross Profit ÷ Revenues". */
export function formula(citation: CitationView): string {
  if (citation.concepts.length < 2) return '';
  return citation.expression
    .replace(/c(\d+)/g, (_, i: string) => {
      const concept = citation.concepts[Number(i)];
      return concept ? (concept.label ?? concept.name) : `c${i}`;
    })
    .replace(/\s\/\s/g, ' ÷ ')
    .replace(/\s\*\s/g, ' × ');
}
