import React from "react";
import { ActivityIndicator, StyleSheet, Text, View } from "react-native";

const GRADE_COLORS = {
  A: "#12b76a",
  B: "#84cc16",
  C: "#f79009",
  D: "#f97066",
  F: "#d92d20",
};

function formatDose(doseAmount, doseUnit) {
  if (doseAmount == null) return null;
  return `${doseAmount}${doseUnit ? ` ${doseUnit}` : ""}`;
}

/** Builds a one-line PubMed literature summary from a completed IngredientGradeSchema. */
function literatureSummary(grade) {
  if (!grade) return null;
  const summary = grade.evidence_summary;
  const claims = grade.validated_claims || [];
  const paperCount = summary?.total_papers_analyzed ?? 0;

  if (paperCount === 0) return "No supporting literature found.";

  const topClaim = [...claims].sort((a, b) => b.supporting_studies_count - a.supporting_studies_count)[0];
  const paperWord = paperCount === 1 ? "paper" : "papers";
  if (!topClaim) return `${paperCount} ${paperWord} reviewed.`;

  const direction =
    topClaim.consensus_score > 0.3
      ? "supports"
      : topClaim.consensus_score < -0.3
        ? "contradicts"
        : "shows mixed evidence for";

  return (
    `${paperCount} ${paperWord} reviewed -- evidence ${direction} "${topClaim.claim}" ` +
    `(${topClaim.supporting_studies_count} studies, ${topClaim.evidence_level.toLowerCase()} confidence).`
  );
}

/** One ingredient's row: name, dose, status, and (once complete) its SIFG grade. */
export default function IngredientCard({ ingredientId, entry }) {
  const status = entry?.status || "pending";
  const dose = formatDose(entry?.dose_amount, entry?.dose_unit);
  const grade = entry?.grade;

  return (
    <View style={styles.card}>
      <View style={styles.headerRow}>
        <Text style={styles.name}>{entry?.raw_name || ingredientId}</Text>
        {status === "completed" && grade && (
          <View
            style={[
              styles.gradeBadge,
              { backgroundColor: GRADE_COLORS[grade.evidence_summary.evidence_grade] || "#98a2b3" },
            ]}
          >
            <Text style={styles.gradeBadgeText}>{grade.evidence_summary.evidence_grade}</Text>
          </View>
        )}
      </View>

      {dose && <Text style={styles.dose}>Dose per serving: {dose}</Text>}

      {status === "pending" || status === "processing" ? (
        <View style={styles.statusRow}>
          <ActivityIndicator size="small" color="#98a2b3" />
          <Text style={styles.statusText}>
            {status === "pending" ? "Waiting to grade..." : "Grading literature..."}
          </Text>
        </View>
      ) : status === "failed" ? (
        <Text style={styles.errorText}>Grading failed: {entry?.error || "Unknown error."}</Text>
      ) : (
        grade && (
          <View>
            <Text style={styles.score}>
              SIFG Score: {grade.evidence_summary.composite_score.toFixed(1)} / 100
            </Text>
            <Text style={styles.summary}>{literatureSummary(grade)}</Text>
          </View>
        )
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  card: {
    backgroundColor: "#ffffff",
    borderRadius: 10,
    borderWidth: 1,
    borderColor: "#e4e7ec",
    padding: 14,
    marginBottom: 10,
  },
  headerRow: { flexDirection: "row", justifyContent: "space-between", alignItems: "center" },
  name: { fontSize: 16, fontWeight: "700", color: "#101828", flexShrink: 1 },
  dose: { color: "#667085", marginTop: 2, marginBottom: 6 },
  gradeBadge: { width: 32, height: 32, borderRadius: 16, alignItems: "center", justifyContent: "center" },
  gradeBadgeText: { color: "#ffffff", fontWeight: "800" },
  statusRow: { flexDirection: "row", alignItems: "center", gap: 8, marginTop: 4 },
  statusText: { color: "#667085" },
  errorText: { color: "#d92d20", marginTop: 4 },
  score: { fontWeight: "700", color: "#3730a3", marginTop: 4 },
  summary: { color: "#344054", marginTop: 4, lineHeight: 20 },
});
