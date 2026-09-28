import { httpResource } from '@angular/common/http';
import { Component } from '@angular/core';
import { MatToolbarModule } from '@angular/material/toolbar';
import { RouterOutlet } from '@angular/router';

import { apiPath } from './api/api.service';
import type { Health } from './api/types';

@Component({
  imports: [MatToolbarModule, RouterOutlet],
  selector: 'vf-root',
  styleUrl: './app.scss',
  templateUrl: './app.html',
})
export class App {
  // Same-origin /api, forwarded by proxy.dev.json in development.
  protected readonly health = httpResource<Health>(() => apiPath('/api/health'));
}
