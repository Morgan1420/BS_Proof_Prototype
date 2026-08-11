import React from "react";
import { StyleSheet, Text, View } from "react-native";
import IngredientRow from "./IngredientRow";

/** Product header + one IngredientRow per ingredient extracted from a single scan. */
export default function ResultsView({ scanResult }) {
  if (!scanResult) return null;

  const { product, ingredients } = scanResult;

  return (
    <View style={styles.container}>
      <View style={styles.productHeader}>
        <Text style={styles.productName}>{product.product_name || "Unknown product"}</Text>
        {product.brand_name && <Text style={styles.brandName}>{product.brand_name}</Text>}
        {product.serving_size && (
          <Text style={styles.servingInfo}>
            Serving size: {product.serving_size}
            {product.servings_per_container != null
              ? ` — ${product.servings_per_container} servings per container`
              : ""}
          </Text>
        )}
      </View>

      {ingredients.length === 0 ? (
        <Text style={styles.empty}>No ingredients were extracted from this label.</Text>
      ) : (
        ingredients.map((ingredient, index) => (
          <IngredientRow key={`${ingredient.name}-${index}`} ingredient={ingredient} />
        ))
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  container: { marginTop: 8 },
  productHeader: { marginBottom: 16 },
  productName: { fontSize: 20, fontWeight: "800", color: "#101828" },
  brandName: { fontSize: 14, color: "#667085", marginTop: 2 },
  servingInfo: { fontSize: 13, color: "#667085", marginTop: 6 },
  empty: { color: "#667085" },
});
