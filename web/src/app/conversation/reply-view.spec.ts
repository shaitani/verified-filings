import { TestBed } from '@angular/core/testing';

import type { AnswerIn, Reply } from '../api/types';
import { REPLIES } from './conversation.testing';
import { ReplyView } from './reply-view';

describe('ReplyView', () => {
  async function shown(reply: Reply, inputs: Record<string, unknown> = {}) {
    TestBed.configureTestingModule({ imports: [ReplyView] });
    const fixture = TestBed.createComponent(ReplyView);
    fixture.componentRef.setInput('reply', reply);
    for (const [name, value] of Object.entries(inputs)) fixture.componentRef.setInput(name, value);
    const sent: (readonly AnswerIn[])[] = [];
    fixture.componentInstance.answered.subscribe((picks) => sent.push(picks));
    await fixture.whenStable();
    const page = fixture.nativeElement as HTMLElement;
    const text = () => (page.textContent ?? '').replace(/\s+/g, ' ');
    const continueButton = () =>
      [...page.querySelectorAll('button')].find((b) => b.textContent?.includes('Continue'));
    return { fixture, page, text, sent, continueButton };
  }

  it('shows each part: the figures it answered, and the reason for the one it refused', async () => {
    const { page, text } = await shown(REPLIES.partial);
    expect(page.querySelectorAll('.parts > li.answered')).toHaveLength(5);
    const refused = page.querySelector('.parts > li.refused')!;
    expect(refused.textContent).toContain('goodwill');
    expect(refused.querySelector('.reason')?.textContent?.trim()).toBe(
      REPLIES.partial.parts.find((p) => p.text === 'goodwill')?.reason,
    );
    expect(text()).toContain(REPLIES.partial.answer!.rows[0].display); // as the Presenter wrote it
  });

  it('shows a blocking refusal first, as an alert', async () => {
    const { page } = await shown(REPLIES.blocking);
    const alert = page.querySelector('[role=alert]');
    expect(alert?.textContent).toContain(REPLIES.blocking.blocking!.reason);
    expect(page.querySelectorAll('.parts > li')).toHaveLength(0);
  });

  it('sends a pick only once every question has one', async () => {
    const { fixture, page, sent, continueButton } = await shown(REPLIES.clarification, {
      answerable: true,
    });
    const ask = REPLIES.clarification.parts[0].ask!;
    expect(continueButton()?.disabled).toBe(true);

    const radios = page.querySelectorAll<HTMLInputElement>('input[type=radio]');
    expect(radios).toHaveLength(ask.options.length);
    radios[1].click();
    await fixture.whenStable();
    expect(continueButton()?.disabled).toBe(false);

    continueButton()!.click();
    expect(sent).toEqual([
      [{ kind: 'option', ask_id: ask.ask_id, option_id: ask.options[1].option_id }],
    ]);
  });

  it('tags an ambiguity, and only an ambiguity', async () => {
    const ambiguity = await shown(REPLIES.ambiguity, { answerable: true });
    expect(ambiguity.page.querySelector('.tag')?.textContent?.trim()).toBe('ambiguous');
    TestBed.resetTestingModule();
    const curated = await shown(REPLIES.clarification, { answerable: true });
    expect(curated.page.querySelector('.tag')).toBeNull();
  });

  it("shows an earlier round's question with its pick, closed", async () => {
    const ask = REPLIES.clarification.parts[0].ask!;
    const picked = ask.options[0].option_id;
    const { page, continueButton } = await shown(REPLIES.clarification, {
      answerable: false,
      chosen: new Map([[ask.ask_id, picked]]),
    });
    const radios = [...page.querySelectorAll<HTMLInputElement>('input[type=radio]')];
    expect(radios.every((radio) => radio.disabled)).toBe(true);
    expect(radios.findIndex((radio) => radio.checked)).toBe(0);
    expect(continueButton()).toBeUndefined();
  });
});
