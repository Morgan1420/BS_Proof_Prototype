import type { Ingredient } from '../components/IngredientCard';

/**
 * Ingredient list filtering (status + category) — backs the
 * `IngredientFilter.tsx` popover and `ResultsScreen.tsx`'s filtered
 * results list.
 *
 * **Category classification is client-side keyword matching on
 * `ingredient.name`, not a real backend category field.** The task spec
 * that introduced this explicitly calls for this fallback ("If
 * ingredients don't have explicit category tags in database schema yet,
 * use keyword matching on `ingredient.name`") — this codebase's
 * `Ingredient` model (`backend/app/models/supplement.py`) genuinely has
 * no category/tag column today, so this is the only option available,
 * not a shortcut taken over a real alternative. If a real
 * `Ingredient.category` column is ever added, `CATEGORY_MATCHERS` below
 * is the one place a name-keyword fallback would be replaced with a
 * direct field read.
 *
 * **`GRADED`/`UNGRADED` read `ingredient.is_graded` only.** The task's
 * own reference `matchesFilter` also checks `ingredient.overall_grade` —
 * but no `overall_grade` field exists anywhere in this codebase (not on
 * the backend `Ingredient` model, not on the frontend `Ingredient`
 * interface in `IngredientCard.tsx`) — `is_graded` is the actual,
 * already-wired boolean every ingredient card already tracks (see that
 * interface's own doc-comment), so the extra `overall_grade` check is
 * dropped rather than referencing a field that would always be
 * `undefined`.
 */

export type FilterType =
  | 'ALL'
  | 'GRADED'
  | 'UNGRADED'
  | 'VITAMINS'
  | 'ENZYMES'
  | 'COLLAGEN'
  | 'OTHER';

/** Display label for each filter — used by both the popover's option
 * list and the active-filter indicator badge on the filter button
 * itself (e.g. "Filter: Vitamins (10)"). */
export const FILTER_LABELS: Record<FilterType, string> = {
  ALL: 'Show All',
  GRADED: 'Graded',
  UNGRADED: 'Ungraded',
  VITAMINS: 'Vitamins & Coenzymes',
  ENZYMES: 'Digestive Enzymes',
  COLLAGEN: 'Collagen & Proteins',
  OTHER: 'Specialty Actives & Other',
};

/** Grouped, ordered option list for the popover — `ALL` first as its own
 * section (per spec: "Top option & default fallback"), then the two
 * status filters, then the four category filters, matching the task's
 * literal ordering. */
export const FILTER_GROUPS: ReadonlyArray<{ label: string; options: FilterType[] }> = [
  { label: '', options: ['ALL'] },
  { label: 'Status', options: ['GRADED', 'UNGRADED'] },
  { label: 'Category', options: ['VITAMINS', 'ENZYMES', 'COLLAGEN', 'OTHER'] },
];

const VITAMIN_PATTERN =
  /vitamin|folate|niacin|riboflavin|thiamin|pantothenic|biotin|ascorbic/;
const ENZYME_PATTERN = /enzyme|amylase|protease|lipase|cellulase|lactase/;
const COLLAGEN_PATTERN = /collagen|peptide|bone broth|protein/;

/**
 * True iff `ingredient` belongs in `filter`'s bucket — see module
 * docstring for the "why keyword matching on name" and "why no
 * `overall_grade` check" notes.
 *
 * `OTHER` deliberately re-derives the other three category checks (via
 * recursive calls, matching the task's own reference implementation)
 * rather than inlining the three regexes a second time — keeps
 * "everything not already classified" in exact sync with the three real
 * category matchers above it, with no risk of the two drifting apart.
 */
export function matchesFilter(ingredient: Ingredient, filter: FilterType): boolean {
  const name = ingredient.name.toLowerCase();

  switch (filter) {
    case 'ALL':
      return true;
    case 'GRADED':
      return Boolean(ingredient.is_graded);
    case 'UNGRADED':
      return !ingredient.is_graded;
    case 'VITAMINS':
      return VITAMIN_PATTERN.test(name);
    case 'ENZYMES':
      return ENZYME_PATTERN.test(name);
    case 'COLLAGEN':
      return COLLAGEN_PATTERN.test(name);
    case 'OTHER':
      return (
        !matchesFilter(ingredient, 'VITAMINS') &&
        !matchesFilter(ingredient, 'ENZYMES') &&
        !matchesFilter(ingredient, 'COLLAGEN')
      );
    default:
      return true;
  }
}
