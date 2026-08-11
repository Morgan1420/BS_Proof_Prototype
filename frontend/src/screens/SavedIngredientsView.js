import React, { useCallback, useEffect, useState } from "react";
import { ActivityIndicator, StyleSheet, Text, View } from "react-native";
import { getIngredients, gradeIngredient } from "../api/client";
import IngredientRow from "../components/IngredientRow";

function formatScannedAt(isoString) {
  if (!isoString) return "";
  const date = new Date(isoString);
  if (Number.isNaN(date.getTime())) return isoString;
  return date.toLocaleString();
}

/**
 * "Saved Ingredients" tab: calls GET /api/ingredients on mount and
 * renders every scan saved so far, read straight from the backend's
 * data/scanned_ingredients.json -- each scan's product info plus every
 * ingredient extracted from that one label image.
 */
export default function SavedIngredientsView() {
  const [status, setStatus] = useState("loading"); // "loading" | "loaded" | "error"
  const [scans, setScans] = useState([]);
  const [errorMessage, setErrorMessage] = useState(null);
  // ingredient_id -> true while a grade request for that one row is in flight.
  const [gradingIds, setGradingIds] = useState(() => new Set());
  // ingredient_id -> last grading error message for that row, if any.
  const [gradeErrors, setGradeErrors] = useState({});

  const load = useCallback(async () => {
    setStatus("loading");
    setErrorMessage(null);
    try {
      const data = await getIngredients();
      setScans(data);
      setStatus("loaded");
    } catch (err) {
      setErrorMessage(err.response?.data?.detail || err.message || "Failed to load saved ingredients.");
      setStatus("error");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const handleGrade = useCallback(async (ingredientId) => {
    if (!ingredientId) return;

    setGradingIds((prev) => new Set(prev).add(ingredientId));
    setGradeErrors((prev) => {
      if (!(ingredientId in prev)) return prev;
      const next = { ...prev };
      delete next[ingredientId];
      return next;
    });

    try {
      const updatedIngredient = await gradeIngredient(ingredientId);
      setScans((prevScans) =>
        prevScans.map((scan) => ({
          ...scan,
          ingredients: scan.ingredients.map((ingredient) =>
            ingredient.ingredient_id === ingredientId ? updatedIngredient : ingredient
          ),
        }))
      );
    } catch (err) {
      const message = err.response?.data?.detail || err.message || "Grading failed.";
      setGradeErrors((prev) => ({ ...prev, [ingredientId]: message }));
    } finally {
      setGradingIds((prev) => {
        const next = new Set(prev);
        next.delete(ingredientId);
        return next;
      });
    }
  }, []);

  return (
    <View style={styles.container}>
      <View style={styles.headerRow}>
        <Text style={styles.title}>Saved Ingredients</Text>
        <Text style={styles.refreshLink} onPress={load}>
          Refresh
        </Text>
      </View>

      {status === "loading" && (
        <View style={styles.centeredRow}>
          <ActivityIndicator size="small" color="#4f46e5" />
          <Text style={styles.loadingText}>Loading saved ingredients...</Text>
        </View>
      )}

      {status === "error" && (
        <View style={styles.errorBox}>
          <Text style={styles.errorBoxText}>{errorMessage}</Text>
        </View>
      )}

      {status === "loaded" && scans.length === 0 && (
        <Text style={styles.empty}>No scans saved yet -- scan a label to get started.</Text>
      )}

      {status === "loaded" &&
        scans.map((scan) => (
          <View key={scan.scan_id} style={styles.scanCard}>
            <Text style={styles.productName}>{scan.product?.product_name || "Unknown product"}</Text>
            {scan.product?.brand_name && <Text style={styles.brandName}>{scan.product.brand_name}</Text>}
            <Text style={styles.scannedAt}>{formatScannedAt(scan.scanned_at)}</Text>

            {scan.ingredients.length === 0 ? (
              <Text style={styles.emptyIngredients}>No ingredients extracted from this label.</Text>
            ) : (
              <View style={styles.ingredientList}>
                {scan.ingredients.map((ingredient, index) => (
                  <IngredientRow
                    key={ingredient.ingredient_id || `${scan.scan_id}-${index}`}
                    ingredient={ingredient}
                    grading={gradingIds.has(ingredient.ingredient_id)}
                    gradeError={gradeErrors[ingredient.ingredient_id]}
                    onGrade={handleGrade}
                  />
                ))}
              </View>
            )}
          </View>
        ))}
    </View>
  );
}

const styles = StyleSheet.create({
  container: {},
  headerRow: { flexDirection: "row", justifyContent: "space-between", alignItems: "center", marginBottom: 16 },
  title: { fontSize: 22, fontWeight: "800", color: "#101828" },
  refreshLink: { color: "#4f46e5", fontWeight: "600" },
  centeredRow: { flexDirection: "row", alignItems: "center", gap: 8, marginTop: 8 },
  loadingText: { color: "#344054" },
  errorBox: {
    backgroundColor: "#fef3f2",
    borderColor: "#fda29b",
    borderWidth: 1,
    borderRadius: 8,
    padding: 12,
  },
  errorBoxText: { color: "#b42318" },
  empty: { color: "#667085" },
  scanCard: {
    backgroundColor: "#f9fafb",
    borderRadius: 12,
    borderWidth: 1,
    borderColor: "#e4e7ec",
    padding: 14,
    marginBottom: 16,
  },
  productName: { fontSize: 17, fontWeight: "800", color: "#101828" },
  brandName: { fontSize: 13, color: "#667085", marginTop: 2 },
  scannedAt: { fontSize: 12, color: "#98a2b3", marginTop: 4, marginBottom: 12 },
  emptyIngredients: { color: "#667085" },
  ingredientList: { marginTop: 2 },
});
