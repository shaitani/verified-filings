import { DatePipe } from '@angular/common';
import { Component, OnInit, inject } from '@angular/core';
import { MatButtonModule } from '@angular/material/button';
import { MatDialog } from '@angular/material/dialog';
import { MatIconModule } from '@angular/material/icon';
import { MatMenuModule } from '@angular/material/menu';
import { MatTableModule } from '@angular/material/table';
import { MatTabsModule } from '@angular/material/tabs';
import { MatTooltipModule } from '@angular/material/tooltip';
import { firstValueFrom } from 'rxjs';

import type { AdminInvite, AdminUser, InviteStatus } from '../api/types';
import {
  ConfirmData,
  ConfirmDialog,
  NewInviteDialog,
  ShownOnceData,
  ShownOnceDialog,
} from './admin-dialogs';
import { AdminStore, INVITE_TABS } from './admin-store';

/** The note on an administrator's row, whose actions are greyed out. */
export const ADMIN_ROW_NOTE = "Administrators are changed from the server's command line.";

const INVITE_TAB_TITLES: Readonly<Record<InviteStatus, string>> = {
  open: 'Open',
  used: 'Used',
  revoked: 'Revoked',
  expired: 'Expired',
};

/**
 * `/admin` -- administrators only (adminGuard; the server refuses anyone else anyway).
 * Users: reset a password, delete an account; deactivate, reactivate and end sessions sit
 * apart in a small "debug" menu. Invites: make one, revoke an open one.
 */
@Component({
  selector: 'vf-admin-page',
  imports: [
    DatePipe,
    MatButtonModule,
    MatIconModule,
    MatMenuModule,
    MatTableModule,
    MatTabsModule,
    MatTooltipModule,
  ],
  providers: [AdminStore],
  templateUrl: './admin-page.html',
  styleUrl: './admin-page.scss',
})
export class AdminPage implements OnInit {
  protected readonly store = inject(AdminStore);
  private readonly dialog = inject(MatDialog);

  protected readonly adminRowNote = ADMIN_ROW_NOTE;
  protected readonly inviteTabs = INVITE_TABS;
  protected readonly inviteTabTitles = INVITE_TAB_TITLES;
  protected readonly userColumns = ['email', 'joined', 'status', 'sessions', 'today', 'actions'];

  /** The columns an invite tab shows: what happened to it, and what can be done. */
  protected inviteColumns(status: InviteStatus): string[] {
    const happened = { open: [], used: ['usedBy', 'usedAt'], revoked: ['revokedAt'], expired: [] };
    const action = status === 'open' ? ['revoke'] : [];
    return ['created', 'expires', ...happened[status], 'madeBy', ...action];
  }

  ngOnInit(): void {
    void this.store.load();
  }

  private async confirmed(data: ConfirmData): Promise<boolean> {
    const ref = this.dialog.open<ConfirmDialog, ConfirmData, boolean>(ConfirmDialog, {
      data,
      width: '440px',
    });
    return (await firstValueFrom(ref.afterClosed())) === true;
  }

  private showOnce(data: ShownOnceData): void {
    this.dialog.open<ShownOnceDialog, ShownOnceData>(ShownOnceDialog, { data, width: '440px' });
  }

  // -- users -------------------------------------------------------------------------

  protected async resetPassword(user: AdminUser): Promise<void> {
    const sure = await this.confirmed({
      title: 'Reset password',
      body: `${user.email} gets a new random password and is signed out everywhere. You hand them the new one.`,
      confirm: 'Reset password',
    });
    if (!sure) return;
    const password = await this.store.resetPassword(user);
    if (password) {
      this.showOnce({
        title: 'New password',
        label: `For ${user.email}:`,
        value: password,
        note: 'Hand it to them yourself.',
      });
    }
  }

  protected async deleteUser(user: AdminUser): Promise<void> {
    const sure = await this.confirmed({
      title: 'Delete account',
      body:
        `${user.email} is signed out and deleted for good, with every question they asked, ` +
        'its traces and their problem reports. This cannot be undone.',
      confirm: 'Delete for good',
      danger: true,
      typed: user.email,
    });
    if (sure) await this.store.deleteUser(user);
  }

  protected async deactivate(user: AdminUser): Promise<void> {
    const sure = await this.confirmed({
      title: 'Deactivate (debug)',
      body: `${user.email} is signed out and cannot sign in until reactivated.`,
      confirm: 'Deactivate',
    });
    if (sure) await this.store.deactivate(user);
  }

  protected async reactivate(user: AdminUser): Promise<void> {
    await this.store.reactivate(user);
  }

  protected async endSessions(user: AdminUser): Promise<void> {
    const sure = await this.confirmed({
      title: 'End sessions (debug)',
      body: `${user.email} is signed out everywhere and may sign in again.`,
      confirm: 'End sessions',
    });
    if (sure) await this.store.endSessions(user);
  }

  // -- invites -------------------------------------------------------------------------

  protected async newInvite(): Promise<void> {
    const ref = this.dialog.open<NewInviteDialog, void, number>(NewInviteDialog, {
      width: '400px',
    });
    const days = await firstValueFrom(ref.afterClosed());
    if (!days) return;
    const code = await this.store.createInvite(days);
    if (code) {
      this.showOnce({
        title: 'New invite code',
        label: `Single use, valid ${days} day${days === 1 ? '' : 's'}:`,
        value: code,
        note: 'Hand it to the person yourself: nothing is emailed.',
      });
    }
  }

  protected async revokeInvite(invite: AdminInvite): Promise<void> {
    const sure = await this.confirmed({
      title: 'Revoke invite',
      body: 'This code can no longer be used to register.',
      confirm: 'Revoke',
    });
    if (sure) await this.store.revokeInvite(invite);
  }
}
