import React, { useState } from "react";
import { ActivityIndicator, Pressable, StyleSheet, Text, View } from "react-native";

function formatAmount(amount, unit) {
  if (amount == null) return null;
  return `${amount}${unit ? ` ${unit}` : ""}`;
}

function formatDuration(seconds) {
  if (seconds == null) return null;
  const n = Number(seconds);
  if (Number.isNaN(n)) return null;
  return `${n.toFixed(2)}s`;
}

/** Rough color coding for a SIFG letter grade -- "A..." reads as strongest, "F"/no
 * clean letter (e.g. "Insufficient Evidence") falls back to a neutral gray. */
function gradeBadgeColors(sifgGrade) {
  const letter = (sifgGrade || "").trim().charAt(0).toUpperCase();
  if (letter === "A") return { bg: "#ecfdf3", fg: "#027a48", border: "#abefc6" };
  if (letter === "B") return { bg: "#eff8ff", fg: "#175cd3", border: "#b2ddff" };
  if (letter === "C") return { bg: "#fffaeb", fg: "#b54708", border: "#fedf89" };
  if (letter === "D" || letter === "F") return { bg: "#fef3f2", fg: "#b42318", border: "#fda29b" };
  return { bg: "#f2f4f7", fg: "#344054", border: "#d0d5dd" };
}

/**
 * One ingredient row: name, form, amount/dosage, and % Daily Value exactly
 * as printed on the label, plus this ingredient's on-demand SIFG grading
 * state (see POST /api/ingredients/{ingredient_id}/grade):
 *
 *   - grade_status "pending" (or missing): a "Grade" button.
 *   - a grade in flight for this row (`grading` prop): the button is
 *     disabled and shows a spinner in place of its label.
 *   - grade_status "failed": a "Retry Grade" button plus the last error.
 *   - grade_status "graded": the button is replaced by a grade badge plus
 *     an expandable "View Grading Details" section -- the backend-computed
 *     grading_stats (papers found/analyzed, search queries, processing
 *     time, model used -- see app.services.grading_service.GradingStats),
 *     the full reasoning summary, and the raw JSON Gemini returned.
 *
 * Grading network calls and per-row loading/error state are owned by the
 * parent screen (see SavedIngredientsView) -- this component is purely
 * presentational plus the local expand/collapse toggle.
 */
export default function IngredientRow({ ingredient, grading, gradeError, onGrade }) {
  const [showDetails, setShowDetails] = useState(false);
  const amount = formatAmount(ingredient.amount, ingredient.unit);
  const isGraded = ingredient.grade_status === "graded";
  const isFailed = ingredient.grade_status === "failed";

  return (
    <View style={styles.row}>
      <View style={styles.headerRow}>
        <Text style={styles.name}>{ingredient.name}</Text>
        {ingredient.percent_daily_value && (
          <View style={styles.dvBadge}>
            <Text style={styles.dvBadgeText}>{ingredient.percent_daily_value} DV</Text>
          </View>
        )}
      </View>
      {ingredient.form && <Text style={styles.form}>{ingredient.form}</Text>}
      {amount && <Text style={styles.amount}>{amount}</Text>}

      <View style={styles.gradeSection}>
        {isGraded ? (
          <GradedState
            ingredient={ingredient}
            showDetails={showDetails}
            onToggleDetails={() => setShowDetails((v) => !v)}
          />
        ) : (
          <UngradedState
            isFailed={isFailed}
            grading={grading}
            gradeError={gradeError}
            onPress={() => onGrade?.(ingredient.ingredient_id)}
          />
        )}
      </View>
    </View>
  );
}

function UngradedState({ isFailed, grading, gradeError, onPress }) {
  return (
    <View>
      <Pressable
        onPress={onPress}
        disabled={grading}
        style={({ pressed }) => [
          styles.gradeButton,
          isFailed && styles.gradeButtonRetry,
          grading && styles.gradeButtonDisabled,
          pressed && !grading && styles.gradeButtonPressed,
        ]}
      >
        {grading && <ActivityIndicator size="small" color="#ffffff" style={styles.spinner} />}
        <Text style={styles.gradeButtonLabel}>
          {grading ? "Grading..." : isFailed ? "Retry Grade" : "Grade"}
        </Text>
      </Pressable>
      {gradeError && !grading && <Text style={styles.gradeErrorText}>{gradeError}</Text>}
    </View>
  );
}

function GradedState({ ingredient, showDetails, onToggleDetails }) {
  const colors = gradeBadgeColors(ingredient.sifg_grade);
  const hasExpandableContent = ingredient.raw_consensus || ingredient.grading_stats;

  return (
    <View>
      <View style={styles.gradedHeaderRow}>
        <View style={[styles.sifgBadge, { backgroundColor: colors.bg, borderColor: colors.border }]}>
          <Text style={[styles.sifgBadgeText, { color: colors.fg }]}>
            {ingredient.sifg_grade}
            {ingredient.sifg_score != null ? ` · ${ingredient.sifg_score}/100` : ""}
          </Text>
        </View>
      </View>

      {ingredient.evidence_summary && <Text style={styles.evidenceSummary}>{ingredient.evidence_summary}</Text>}

      {hasExpandableContent && (
        <View>
          <Text style={styles.detailsToggle} onPress={onToggleDetails}>
            {showDetails ? "Hide grading details" : "View grading details"}
          </Text>
          {showDetails && (
            <View>
              {ingredient.grading_stats && <GradingStatsCard stats={ingredient.grading_stats} />}

              <Text style={styles.sectionLabel}>Reasoning Summary</Text>
              <View style={styles.reasoningBox}>
                {ingredient.efficacy_safety_evaluation && (
                  <Text style={styles.gradeDetailLabel}>
                    Efficacy & Safety:{" "}
                    <Text style={styles.gradeDetailText}>{ingredient.efficacy_safety_evaluation}</Text>
                  </Text>
                )}
                {ingredient.dosage_appropriateness && (
                  <Text style={styles.gradeDetailLabel}>
                    Dosage: <Text style={styles.gradeDetailText}>{ingredient.dosage_appropriateness}</Text>
                  </Text>
                )}
                {ingredient.evidence_summary && (
                  <Text style={styles.gradeDetailLabel}>
                    Evidence: <Text style={styles.gradeDetailText}>{ingredient.evidence_summary}</Text>
                  </Text>
                )}
              </View>

              {ingredient.raw_consensus && (
                <View>
                  <Text style={styles.sectionLabel}>Raw JSON</Text>
                  <View style={styles.rawJsonBox}>
                    <Text style={styles.rawJsonText}>{JSON.stringify(ingredient.raw_consensus, null, 2)}</Text>
                  </View>
                </View>
              )}
            </View>
          )}
        </View>
      )}
    </View>
  );
}

/** "Grading Stats" card: papers found/analyzed, search queries, processing time, model used --
 * all backend-computed metadata about the grading run itself (see
 * app.services.grading_service.GradingStats), kept visually distinct from the
 * "Reasoning Summary" / raw JSON below it, which is Gemini's own output. */
function GradingStatsCard({ stats }) {
  const duration = formatDuration(stats.grading_duration_seconds);
  return (
    <View style={styles.statsCard}>
      <Text style={styles.statsCardTitle}>Grading Stats</Text>
      <StatRow label="Papers Found" value={stats.papers_found} />
      <StatRow label="Papers Analyzed" value={stats.papers_analyzed} />
      <StatRow label="Processing Time" value={duration} />
      <StatRow label="Model Used" value={stats.model_used} />
      {stats.provider_counts && Object.keys(stats.provider_counts).length > 0 && (
        <View style={styles.statsQueriesBlock}>
          <Text style={styles.statLabel}>Sources</Text>
          <Text style={styles.statsQueryText}>
            {Object.entries(stats.provider_counts)
              .map(([provider, count]) => `${provider}: ${count}`)
              .join(" · ")}
          </Text>
        </View>
      )}
      {stats.search_queries && stats.search_queries.length > 0 && (
        <View style={styles.statsQueriesBlock}>
          <Text style={styles.statLabel}>Search Queries</Text>
          {stats.search_queries.map((query, index) => (
            <Text key={`${query}-${index}`} style={styles.statsQueryText}>
              • {query}
            </Text>
          ))}
        </View>
      )}
    </View>
  );
}

function StatRow({ label, value }) {
  if (value == null || value === "") return null;
  return (
    <View style={styles.statRow}>
      <Text style={styles.statLabel}>{label}</Text>
      <Text style={styles.statValue}>{value}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  row: {
    backgroundColor: "#ffffff",
    borderRadius: 10,
    borderWidth: 1,
    borderColor: "#e4e7ec",
    padding: 14,
    marginBottom: 10,
  },
  headerRow: { flexDirection: "row", justifyContent: "space-between", alignItems: "flex-start" },
  name: { fontSize: 16, fontWeight: "700", color: "#101828", flexShrink: 1 },
  form: { color: "#3730a3", marginTop: 2 },
  amount: { color: "#667085", marginTop: 2 },
  dvBadge: {
    backgroundColor: "#eef2ff",
    borderRadius: 6,
    paddingVertical: 3,
    paddingHorizontal: 8,
    marginLeft: 8,
  },
  dvBadgeText: { color: "#3730a3", fontWeight: "700", fontSize: 12 },

  gradeSection: { marginTop: 10 },

  gradeButton: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    alignSelf: "flex-start",
    backgroundColor: "#4f46e5",
    paddingVertical: 8,
    paddingHorizontal: 14,
    borderRadius: 8,
  },
  gradeButtonRetry: { backgroundColor: "#b54708" },
  gradeButtonDisabled: { backgroundColor: "#c7c9f5" },
  gradeButtonPressed: { opacity: 0.85 },
  gradeButtonLabel: { color: "#ffffff", fontWeight: "700", fontSize: 13 },
  spinner: { marginRight: 8 },
  gradeErrorText: { color: "#b42318", fontSize: 12, marginTop: 6 },

  gradedHeaderRow: { flexDirection: "row", alignItems: "center" },
  sifgBadge: {
    borderRadius: 8,
    borderWidth: 1,
    paddingVertical: 5,
    paddingHorizontal: 10,
    alignSelf: "flex-start",
  },
  sifgBadgeText: { fontWeight: "800", fontSize: 13 },
  evidenceSummary: { color: "#344054", fontSize: 13, marginTop: 8, lineHeight: 18 },
  gradeDetailLabel: { color: "#101828", fontWeight: "700", fontSize: 12, marginTop: 6 },
  gradeDetailText: { color: "#344054", fontWeight: "400" },

  detailsToggle: { color: "#4f46e5", fontWeight: "600", fontSize: 12, marginTop: 10 },
  sectionLabel: { color: "#101828", fontWeight: "800", fontSize: 12, marginTop: 12, marginBottom: 4 },

  statsCard: {
    backgroundColor: "#f9fafb",
    borderRadius: 8,
    borderWidth: 1,
    borderColor: "#e4e7ec",
    padding: 10,
    marginTop: 8,
  },
  statsCardTitle: { fontWeight: "800", fontSize: 12, color: "#101828", marginBottom: 6 },
  statRow: { flexDirection: "row", justifyContent: "space-between", paddingVertical: 2 },
  statLabel: { color: "#667085", fontSize: 12 },
  statValue: { color: "#101828", fontSize: 12, fontWeight: "600" },
  statsQueriesBlock: { marginTop: 6 },
  statsQueryText: { color: "#344054", fontSize: 12, marginTop: 2 },

  reasoningBox: {
    backgroundColor: "#f9fafb",
    borderRadius: 8,
    borderWidth: 1,
    borderColor: "#e4e7ec",
    padding: 10,
  },

  rawJsonBox: {
    backgroundColor: "#101828",
    borderRadius: 8,
    padding: 10,
  },
  rawJsonText: {
    color: "#d0d5dd",
    fontFamily: "monospace",
    fontSize: 11,
  },
});
