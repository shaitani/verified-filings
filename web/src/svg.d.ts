// An .svg import is its text (angular.json's loader), for the icons in app/icons.ts.
declare module '*.svg' {
  const content: string;
  export default content;
}
