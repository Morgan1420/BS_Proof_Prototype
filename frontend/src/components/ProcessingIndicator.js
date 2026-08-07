import React from "react";
import { ActivityIndicator, StyleSheet, Text, View } from "react-native";

function summarize(phase, ingredientResults) {
  const entries = Object.values(ingredientResults);
  const total = entries.length;
  const settled = entries.filter((e) => e.status === "completed" || e.status === "failed").length;

  if (phase === "uploading") return "Uploading image & extracting ingredients...";
  if (phase === "polling") {
    if (total === 0) return "Grading literature...";
    return `Grading ingredients: ${settled} of ${total} complete`;
  }
  return "Working...";
}

/** Shows upload + background-grading progress while phase is "uploading" or "polling". */
export default function ProcessingIndicator({ phase, ingredientResults }) {
  if (phase !== "uploading" && phase !== "polling") return null;

  return (
    <View style={styles.container}>
      <ActivityIndicator size="small" color="#4f46e5" />
      <Text style={styles.text}>{summarize(phase, ingredientResults)}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flexDirection: "row", alignItems: "center", gap: 8, marginBottom: 16 },
  text: { color: "#344054" },
});
