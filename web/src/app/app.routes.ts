import { Routes } from '@angular/router';

import { adminGuard, signedInGuard, signedOutGuard } from './auth/auth-guards';

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
  {
    path: 'admin', // administrators only: its own bundle, never downloaded by anyone else
    canActivate: [adminGuard],
    loadComponent: () => import('./admin/admin-page').then((m) => m.AdminPage),
  },
  {
    path: 'admin/rounds/:jobId', // one round's trace, linkable
    canActivate: [adminGuard],
    loadComponent: () => import('./admin/trace-page').then((m) => m.TracePage),
  },
  { path: '**', redirectTo: '' },
];
