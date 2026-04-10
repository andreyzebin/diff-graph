package com.flowmart.orders.util;

import java.math.BigDecimal;
import java.text.NumberFormat;

public class OrderUtils {

    public static BigDecimal calculateTax(BigDecimal amount) {
        return amount.multiply(new BigDecimal("0.1"));
    }

    public static boolean isValid(String orderId) {
        return orderId != null && orderId.startsWith("ORD-");
    }

    public static String formatTotal(BigDecimal total) {
        return NumberFormat.getCurrencyInstance().format(total);
    }
}
