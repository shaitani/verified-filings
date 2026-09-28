import { httpResource } from '@angular/common/http';
import { Component, inject } from '@angular/core';
import { MatButtonModule } from '@angular/material/button';
import { MatToolbarModule } from '@angular/material/toolbar';
import { Router, RouterLink, RouterOutlet } from '@angular/router';

import { apiPath } from './api/api.service';
import type { Health } from './api/types';
import { AuthStore } from './auth/auth-store';

@Component({
  imports: [MatButtonModule, MatToolbarModule, RouterLink, RouterOutlet],
  selector: 'vf-root',
  styleUrl: './app.scss',
  templateUrl: './app.html',
})
export class App {
  protected readonly auth = inject(AuthStore);
  private readonly router = inject(Router);

  // Same-origin /api, forwarded by proxy.dev.json in development.
  protected readonly health = httpResource<Health>(() => apiPath('/api/health'));

  protected async signOut(): Promise<void> {
    await this.auth.signOut();
    await this.router.navigateByUrl('/login');
  }
}
