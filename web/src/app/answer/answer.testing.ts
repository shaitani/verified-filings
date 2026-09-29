// Shared by the answer tests; `.testing.ts` keeps it out of the app build.
import type { AnswerView } from '../api/types';
import answers from './fixtures/answers.json';

/**
 * Real AnswerViews: the Presenter over captured ResultSets, written by
 * `uv run python -m tests.web_replies` (pytest keeps them current).
 */
export const ANSWERS = Object.fromEntries(
  Object.entries(answers).map(([name, capture]) => [name, capture.answer as AnswerView]),
) as Record<keyof typeof answers, AnswerView>;
