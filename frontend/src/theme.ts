/**
 * BSProof strict color palette, extracted from image_0.png.
 *
 * Every color used anywhere in the UI should come from this file — do not
 * introduce new hex values in component styles. Where a "muted"/disabled
 * look is needed, use `opacity` on an existing palette color rather than a
 * new gray, to keep the app strictly within this mapping.
 */
export const colors = {
  /** Logo / primary title text. */
  brown: '#8C3703',
  /** Primary action buttons (e.g. "Scan Supplement" in the Hero). */
  orange: '#E85D04',
  /** Secondary action buttons (e.g. "Supplement Library" in the Hero). */
  yellow: '#FFBA08',
  /** Accent background 1 — Hero section background. */
  lightYellow: '#FBD569',
  /** Main app background (default). */
  offWhite: '#F7EFCA',
  /** Accent background 2 — info section image placeholder. */
  olive: '#899536',
  /** NavBar and Footer background. */
  darkGreen: '#355A35',
} as const;

export type ColorKey = keyof typeof colors;

/** Shared font sizes so text scale stays consistent across screens. */
export const typography = {
  navBarLogo: 22,
  navBarLink: 15,
  heroTitle: 40,
  sectionTitle: 22,
  body: 16,
  buttonLabel: 16,
} as const;

/** Shared spacing scale so padding/margins stay consistent across screens. */
export const spacing = {
  xs: 4,
  sm: 8,
  md: 16,
  lg: 24,
  xl: 32,
} as const;
