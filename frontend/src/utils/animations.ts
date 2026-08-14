import { LayoutAnimation, Platform, UIManager } from 'react-native';

// Android requires this experimental flag before LayoutAnimation does
// anything (older/Paper-renderer requirement; harmless to also call it
// under the new Fabric architecture, where it's typically a no-op since
// LayoutAnimation is supported natively there). Guarded on the method
// actually existing, since some RN versions have removed it entirely —
// calling it unconditionally could throw on those.
if (
  Platform.OS === 'android' &&
  UIManager.setLayoutAnimationEnabledExperimental
) {
  UIManager.setLayoutAnimationEnabledExperimental(true);
}

/**
 * Triggers a fast, smooth ease-in-ease-out layout animation for the next
 * layout-affecting state update — used by the accordion-style result
 * cards (ProductCard, IngredientCard) so expanding/collapsing animates
 * instead of snapping instantly.
 *
 * Must be called synchronously, immediately before the state update that
 * changes what's rendered (e.g. right before `setIsExpanded(...)`) — it
 * configures the *next* layout pass, not layouts already in flight.
 */
export function animateCardToggle(): void {
  LayoutAnimation.configureNext(LayoutAnimation.Presets.easeInEaseOut);
}
