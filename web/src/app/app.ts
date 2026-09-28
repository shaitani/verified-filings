import { httpResource } from '@angular/common/http';
import { Component } from '@angular/core';
import { MatToolbarModule } from '@angular/material/toolbar';
import { RouterOutlet } from '@angular/router';

@Component({
  imports: [MatToolbarModule, RouterOutlet],
  selector: 'vf-root',
  styleUrl: './app.scss',
  templateUrl: './app.html',
})
export class App {
  // Same-origin /api, forwarded by proxy.dev.json in development. The inline type
  // gives way to the generated one in slice 2.
  protected readonly health = httpResource<{ status: string }>(() => '/api/health');
}
