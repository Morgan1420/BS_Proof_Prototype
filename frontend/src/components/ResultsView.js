import React from "react";
import { StyleSheet, Text, View } from "react-native";
import IngredientCard from "./IngredientCard";

/** Product header + one IngredientCard per extracted ingredient. */
export default function ResultsView({ job, ingredientResults }) {
  if (!job) return null;

  const product = job.product_metadata;
  const ingredientIds = Object.keys(ingredientResults);

  return (
    <View style={styles.container}>
      <View style={styles.productHeader}>
        <Text style={styles.productName}>{product.product_name}</Text>
        <Text style={styles.brandName}>{product.brand_name}</Text>
      </View>

      {ingredientIds.length === 0 ? (
        <Text style={styles.empty}>No ingredients were extracted from this label.</Text>
      ) : (
        ingredientIds.map((id) => <IngredientCard key={id} ingredientId={id} entry={ingredientResults[id]} />)
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  container: { marginTop: 8 },
  productHeader: { marginBottom: 16 },
  productName: { fontSize: 20, fontWeight: "800", color: "#101828" },
  brandName: { fontSize: 14, color: "#667085", marginTop: 2 },
  empty: { color: "#667085" },
});
