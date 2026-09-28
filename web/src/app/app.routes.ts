import { Routes } from '@angular/router';

import { signedInGuard, signedOutGuard } from './auth/auth-guards';

export const routes: Routes = [
  {
    path: 'login',
    canActivate: [signedOutGuard],
    loadComponent: () => import('./auth/login-page').then((m) => m.LoginPage),
  },
  {
    path: 'register',
    canActivate: [signedOutGuard],
    loadComponent: () => import('./auth/register-page').then((m) => m.RegisterPage),
  },
  {
    // GitHub's return address: GITHUB_OAUTH_REDIRECT_URL must name exactly this path.
    path: 'auth/github/callback',
    loadComponent: () => import('./auth/github-return-page').then((m) => m.GitHubReturnPage),
  },
  {
    path: '',
    pathMatch: 'full',
    canActivate: [signedInGuard],
    loadComponent: () => import('./home/home-page').then((m) => m.HomePage),
  },
  {
    path: 'c/:conversationId', // a conversation, reopenable by its address
    canActivate: [signedInGuard],
    loadComponent: () => import('./conversation/thread-page').then((m) => m.ThreadPage),
  },
  { path: '**', redirectTo: '' },
];
