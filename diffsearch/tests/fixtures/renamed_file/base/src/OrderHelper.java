package com.flowmart.orders.util;

import java.math.BigDecimal;

public class OrderHelper {

    public static BigDecimal calculateTax(BigDecimal amount) {
        return amount.multiply(new BigDecimal("0.1"));
    }

    public static boolean isValid(String orderId) {
        return orderId != null && orderId.startsWith("ORD-");
    }
}
