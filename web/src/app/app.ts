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

/** Too narrow for the banner's words: its buttons become icons, the account a menu. */
const PHONE = '(max-width: 599.98px)';

const THEME_KEY = 'vf-theme';

function readTheme(): string | null {
  try {
    return localStorage.getItem(THEME_KEY);
  } catch {
    return null;
  }
}

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
  // The phone banner's account panel closes on a click anywhere else, or Escape.
  host: {
    '(document:click)': 'closeAccount($event)',
    '(document:keydown.escape)': 'accountOpen.set(false)',
  },
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
  protected readonly phone = toSignal(
    inject(BreakpointObserver)
      .observe(PHONE)
      .pipe(map((state) => state.matches)),
    { initialValue: false },
  );
  protected readonly drawerOpen = signal(false);
  // Not Material's menu: its overlay would add ~70 kB to every reader's first download.
  protected readonly accountOpen = signal(false); // narrow screens only: wide ones always show it

  // Light or dark; remembered in this browser, light until the reader picks otherwise.
  protected readonly dark = signal(readTheme() === 'dark');

  constructor() {
    effect(() => {
      const dark = this.dark();
      document.body.classList.toggle('dark', dark);
      try {
        localStorage.setItem(THEME_KEY, dark ? 'dark' : 'light');
      } catch {
        // Storage can be blocked; the choice then lasts until the page closes.
      }
    });
    // The list belongs to whoever is signed in, and to nobody after they sign out.
    effect(() => {
      if (this.auth.signedIn()) void this.history.refresh();
      else this.history.clear();
    });
  }

  /**
   * Flip the theme as a cross-fade between two pictures of the page (View Transitions), which
   * stays smooth where transitioning every element's colours stutters. Browsers without it,
   * and readers who prefer reduced motion, get an instant switch.
   */
  protected toggleTheme(): void {
    const next = !this.dark();
    const apply = () => {
      this.dark.set(next);
      document.body.classList.toggle('dark', next); // now, not in the effect: the new picture is taken after this
    };
    const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (reduced || !document.startViewTransition) apply();
    else document.startViewTransition(apply);
  }

  protected closeAccount(event: Event): void {
    const inside = (event.target as Element | null)?.closest?.('.account-menu');
    if (!inside) this.accountOpen.set(false);
  }

  protected async signOut(): Promise<void> {
    this.accountOpen.set(false);
    await this.auth.signOut();
    await this.router.navigateByUrl('/login');
  }
}
