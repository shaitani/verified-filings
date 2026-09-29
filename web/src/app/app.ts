import { BreakpointObserver } from '@angular/cdk/layout';
import { httpResource } from '@angular/common/http';
import { Component, effect, inject, signal } from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatSidenavModule } from '@angular/material/sidenav';
import { MatToolbarModule } from '@angular/material/toolbar';
import { Router, RouterLink, RouterOutlet } from '@angular/router';
import { map } from 'rxjs';

import { apiPath } from './api/api.service';
import type { Health } from './api/types';
import { AuthStore } from './auth/auth-store';
import { HistoryList } from './history/history-list';
import { HistoryStore } from './history/history-store';

/** Wide enough for the past questions to sit beside the thread rather than over it. */
const WIDE = '(min-width: 960px)';

@Component({
  imports: [
    HistoryList,
    MatButtonModule,
    MatIconModule,
    MatSidenavModule,
    MatToolbarModule,
    RouterLink,
    RouterOutlet,
  ],
  selector: 'vf-root',
  styleUrl: './app.scss',
  templateUrl: './app.html',
})
export class App {
  protected readonly auth = inject(AuthStore);
  private readonly history = inject(HistoryStore);
  private readonly router = inject(Router);

  // Same-origin /api, forwarded by proxy.dev.json in development.
  protected readonly health = httpResource<Health>(() => apiPath('/api/health'));

  protected readonly wide = toSignal(
    inject(BreakpointObserver)
      .observe(WIDE)
      .pipe(map((state) => state.matches)),
    { initialValue: true },
  );
  protected readonly drawerOpen = signal(false); // narrow screens only: wide ones always show it

  constructor() {
    // The list belongs to whoever is signed in, and to nobody after they sign out.
    effect(() => {
      if (this.auth.signedIn()) void this.history.refresh();
      else this.history.clear();
    });
  }

  protected async signOut(): Promise<void> {
    await this.auth.signOut();
    await this.router.navigateByUrl('/login');
  }
}
