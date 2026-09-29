import { TestBed } from '@angular/core/testing';

import { StageLine } from './stage-line';

describe('StageLine', () => {
  afterEach(() => vi.useRealTimers());

  it('counts the seconds since the round was queued, and keeps counting', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-09-28T12:00:10Z'));
    const fixture = TestBed.createComponent(StageLine);
    fixture.componentRef.setInput('stage', 'fetching');
    fixture.componentRef.setInput('startedAt', new Date('2026-09-28T12:00:00Z').getTime());
    // Drawn by hand: with the clock faked, waiting for the page to settle never ends.
    fixture.detectChanges();
    const text = () => (fixture.nativeElement as HTMLElement).textContent?.replace(/\s+/g, ' ');
    expect(text()).toContain('Fetching the figures… 10 s');

    await vi.advanceTimersByTimeAsync(3000);
    fixture.detectChanges();
    expect(text()).toContain('13 s');
  });

  it('shows no clock until it knows when the round began', async () => {
    const fixture = TestBed.createComponent(StageLine);
    fixture.componentRef.setInput('stage', 'queued');
    await fixture.whenStable();
    expect((fixture.nativeElement as HTMLElement).querySelector('.seconds')).toBeNull();
  });
});
