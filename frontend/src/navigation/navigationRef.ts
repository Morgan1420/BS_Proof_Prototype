import { createNavigationContainerRef } from '@react-navigation/native';

import type { RootStackParamList } from './types';

/**
 * Navigation ref for components rendered OUTSIDE the Stack Navigator's own
 * screen tree — specifically the persistent NavBar, which sits above
 * `<Stack.Navigator>` in src/App.tsx and therefore has no `navigation` prop
 * and can't use the `useNavigation()` hook the way in-stack screens can.
 *
 * See: https://reactnavigation.org/docs/navigating-without-navigation-prop/
 */
export const navigationRef = createNavigationContainerRef<RootStackParamList>();

/**
 * Navigates to a top-level route. All current routes take no params; if a
 * route needs params later, prefer calling `navigationRef.navigate(...)`
 * directly at the call site so its param type is checked.
 */
export function navigateTo(name: keyof RootStackParamList): void {
  if (navigationRef.isReady()) {
    navigationRef.navigate(name as never);
  }
}
