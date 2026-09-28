import { Component, OnInit, inject, signal } from '@angular/core';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';

import { signInProblem } from './auth-errors';
import { AuthStore } from './auth-store';

/**
 * Where GitHub sends the browser back (`GITHUB_OAUTH_REDIRECT_URL` names this page). It
 * hands GitHub's `code` and `state` to the server, which checks them and signs in.
 */
@Component({
  selector: 'vf-github-return-page',
  imports: [MatButtonModule, MatCardModule, RouterLink],
  templateUrl: './github-return-page.html',
  styleUrl: './sign-in-pages.scss',
})
export class GitHubReturnPage implements OnInit {
  private readonly auth = inject(AuthStore);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);

  protected readonly problem = signal<string | null>(null);

  async ngOnInit(): Promise<void> {
    const fromGitHub = this.route.snapshot.queryParams as Record<string, string>;
    if ('error' in fromGitHub) {
      // The reader declined on GitHub's page, or GitHub refused: nothing to hand on.
      this.problem.set('GitHub sign-in was cancelled.');
      return;
    }
    try {
      await this.auth.finishGitHub(fromGitHub);
      await this.router.navigateByUrl('/', { replaceUrl: true }); // the code is single-use
    } catch (error) {
      this.problem.set(signInProblem(error, 'github'));
    }
  }
}
