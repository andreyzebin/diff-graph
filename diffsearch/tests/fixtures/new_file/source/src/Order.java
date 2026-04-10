package com.flowmart.orders.model;

import lombok.Builder;
import lombok.Data;

@Data
@Builder
public class Order {
    private Long id;
    @Builder.Default
    private List<OrderItem> items = new ArrayList<>();
    private OrderStatus status;
    private BigDecimal total;
}
