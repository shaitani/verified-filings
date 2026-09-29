import { HttpErrorResponse } from '@angular/common/http';
import { TestBed } from '@angular/core/testing';
import { MAT_DIALOG_DATA, MatDialogRef } from '@angular/material/dialog';

import { ReportDialog, reportProblem } from './report-dialog';

describe('ReportDialog', () => {
  function opened(send: (note: string | null) => Promise<void>) {
    const close = vi.fn();
    TestBed.configureTestingModule({
      imports: [ReportDialog],
      providers: [
        { provide: MAT_DIALOG_DATA, useValue: { send } },
        { provide: MatDialogRef, useValue: { close } },
      ],
    });
    const fixture = TestBed.createComponent(ReportDialog);
    const page = fixture.nativeElement as HTMLElement;
    const reportButton = () =>
      [...page.querySelectorAll('button')].find((b) => b.textContent?.trim() === 'Report')!;
    return { fixture, page, close, reportButton };
  }

  it('sends the note, trimmed, and closes', async () => {
    const send = vi.fn(async () => undefined);
    const { fixture, page, close, reportButton } = opened(send);
    await fixture.whenStable();
    const box = page.querySelector('textarea')!;
    box.value = '  goodwill looks wrong  ';
    box.dispatchEvent(new Event('input'));
    reportButton().click();
    await fixture.whenStable();
    expect(send).toHaveBeenCalledWith('goodwill looks wrong');
    expect(close).toHaveBeenCalledWith(true);
  });

  it('sends no note when none was written', async () => {
    const send = vi.fn(async () => undefined);
    const { fixture, reportButton } = opened(send);
    await fixture.whenStable();
    reportButton().click();
    await fixture.whenStable();
    expect(send).toHaveBeenCalledWith(null);
  });

  it('stays open and says why when the report fails', async () => {
    const { fixture, page, close, reportButton } = opened(async () => {
      throw new HttpErrorResponse({ status: 503 });
    });
    await fixture.whenStable();
    reportButton().click();
    await fixture.whenStable();
    expect(close).not.toHaveBeenCalled();
    expect(page.querySelector('[role=alert]')?.textContent).toContain('could not be sent');
  });
});

describe('reportProblem', () => {
  it("reads the note's limit off the server's refusal", () => {
    const tooLong = new HttpErrorResponse({
      status: 422,
      error: { detail: [{ type: 'string_too_long', ctx: { max_length: 2000 } }] },
    });
    expect(reportProblem(tooLong)).toBe('A note can be at most 2000 characters.');
  });
});
