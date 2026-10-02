import { inject, provideAppInitializer } from '@angular/core';
import { MatIconRegistry } from '@angular/material/icon';
import { DomSanitizer } from '@angular/platform-browser';

import accountCircle from '@material-symbols/svg-400/outlined/account_circle.svg';
import add from '@material-symbols/svg-400/outlined/add.svg';
import adminPanelSettings from '@material-symbols/svg-400/outlined/admin_panel_settings.svg';
import arrowBack from '@material-symbols/svg-400/outlined/arrow_back.svg';
import arrowUpward from '@material-symbols/svg-400/outlined/arrow_upward.svg';
import block from '@material-symbols/svg-400/outlined/block.svg';
import check from '@material-symbols/svg-400/outlined/check.svg';
import checkCircle from '@material-symbols/svg-400/outlined/check_circle.svg';
import contentCopy from '@material-symbols/svg-400/outlined/content_copy.svg';
import darkMode from '@material-symbols/svg-400/outlined/dark_mode.svg';
import editSquare from '@material-symbols/svg-400/outlined/edit_square.svg';
import error from '@material-symbols/svg-400/outlined/error.svg';
import help from '@material-symbols/svg-400/outlined/help.svg';
import hourglassTop from '@material-symbols/svg-400/outlined/hourglass_top.svg';
import info from '@material-symbols/svg-400/outlined/info.svg';
import lightMode from '@material-symbols/svg-400/outlined/light_mode.svg';
import logout from '@material-symbols/svg-400/outlined/logout.svg';
import menu from '@material-symbols/svg-400/outlined/menu.svg';
import rule from '@material-symbols/svg-400/outlined/rule.svg';
import verified from '@material-symbols/svg-400/outlined/verified.svg';

/**
 * Every icon the client shows, as an SVG drawn in the page -- not the Material Symbols
 * web font. A font can be blocked (a privacy browser's "block web fonts", iPhone's
 * Lockdown Mode), and then `<mat-icon svgIcon="menu" />` shows the word "menu". These are
 * Google's own Material Symbols (Outlined, weight 400; @material-symbols/svg-400,
 * Apache-2.0), imported as text (angular.json's `.svg` loader) and bundled, so they
 * need no request and no font. Use as `<mat-icon svgIcon="menu" />`; a name not
 * listed here shows nothing, so add it here first.
 */
export const ICONS: Readonly<Record<string, string>> = {
  account_circle: accountCircle,
  add,
  admin_panel_settings: adminPanelSettings,
  arrow_back: arrowBack,
  arrow_upward: arrowUpward,
  block,
  check,
  check_circle: checkCircle,
  content_copy: contentCopy,
  dark_mode: darkMode,
  edit_square: editSquare,
  error,
  help,
  hourglass_top: hourglassTop,
  info,
  light_mode: lightMode,
  logout,
  menu,
  rule,
  verified,
};

/** Registers ICONS with Angular Material when the app starts (app.config.ts). */
export function provideIcons() {
  return provideAppInitializer(() => {
    const [registry, sanitizer] = [inject(MatIconRegistry), inject(DomSanitizer)];
    for (const [name, svg] of Object.entries(ICONS)) {
      // Trusted: our own bundled files, never anything a reader or the server sent.
      registry.addSvgIconLiteral(name, sanitizer.bypassSecurityTrustHtml(svg));
    }
  });
}
