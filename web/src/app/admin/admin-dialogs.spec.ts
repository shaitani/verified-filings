import { Type } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { MAT_DIALOG_DATA, MatDialogRef } from '@angular/material/dialog';

import { ConfirmData, ConfirmDialog, NewInviteDialog } from './admin-dialogs';

function open<T>(component: Type<T>, data?: ConfirmData) {
  const close = vi.fn();
  TestBed.configureTestingModule({
    imports: [component],
    providers: [
      { provide: MAT_DIALOG_DATA, useValue: data ?? null },
      { provide: MatDialogRef, useValue: { close } },
    ],
  });
  const fixture = TestBed.createComponent(component);
  const page = fixture.nativeElement as HTMLElement;
  const button = (label: string) =>
    [...page.querySelectorAll('button')].find((b) => b.textContent?.trim() === label)!;
  const type = (value: string) => {
    const input = page.querySelector('input')!;
    input.value = value;
    input.dispatchEvent(new Event('input'));
  };
  return { fixture, close, button, type };
}

describe('ConfirmDialog', () => {
  it('confirms only once the word is typed exactly', async () => {
    const { fixture, close, button, type } = open(ConfirmDialog, {
      title: 'Delete account',
      body: '…',
      confirm: 'Delete for good',
      typed: 'reader@example.com',
    });
    await fixture.whenStable();
    expect(button('Delete for good').disabled).toBe(true);
    type('reader@example');
    await fixture.whenStable();
    expect(button('Delete for good').disabled).toBe(true);
    type(' reader@example.com ');
    await fixture.whenStable();
    button('Delete for good').click();
    expect(close).toHaveBeenCalledWith(true);
  });

  it('needs no typing when no word is asked for', async () => {
    const { fixture, close, button } = open(ConfirmDialog, {
      title: 'Revoke invite',
      body: '…',
      confirm: 'Revoke',
    });
    await fixture.whenStable();
    button('Revoke').click();
    expect(close).toHaveBeenCalledWith(true);
  });
});

describe('NewInviteDialog', () => {
  it('offers 14 days and closes with the days chosen', async () => {
    const { fixture, close, button, type } = open(NewInviteDialog);
    await fixture.whenStable();
    expect(fixture.nativeElement.querySelector('input').value).toBe('14');
    type('30');
    await fixture.whenStable();
    button('Create').click();
    expect(close).toHaveBeenCalledWith(30);
  });

  it.each(['0', '91', '2.5'])('refuses %s days', async (days) => {
    const { fixture, button, type } = open(NewInviteDialog);
    await fixture.whenStable();
    type(days);
    await fixture.whenStable();
    expect(button('Create').disabled).toBe(true);
  });
});
