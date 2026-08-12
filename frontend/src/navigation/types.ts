/** Which table(s) a search/browse request covers. Mirrors the backend's
 * FilterType enum (app/schemas/search.py). */
export type FilterType = 'all' | 'products' | 'ingredients';

/**
 * Route/param definitions for the root Stack Navigator.
 */
export type RootStackParamList = {
  HomeScreen: undefined;
  ScanScreen: undefined;
  LibraryScreen: undefined;
  ResultsScreen: {
    query?: string;
    filterType?: FilterType;
  };
};
