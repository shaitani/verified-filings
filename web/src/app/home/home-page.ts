import { Component, inject } from '@angular/core';

import { AuthStore } from '../auth/auth-store';

/** A placeholder until slice 4 puts the question box here. */
@Component({
  selector: 'vf-home-page',
  template: `<p>Signed in as {{ auth.user()?.email }}. Asking a question arrives in slice 4.</p>`,
})
export class HomePage {
  protected readonly auth = inject(AuthStore);
}
