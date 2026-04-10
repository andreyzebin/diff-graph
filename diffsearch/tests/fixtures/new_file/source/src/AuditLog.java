package com.flowmart.orders.audit;

import java.time.Instant;

public class AuditLog {
    private final Long orderId;
    private final String action;
    private final Instant timestamp;

    public AuditLog(Long orderId, String action) {
        this.orderId = orderId;
        this.action = action;
        this.timestamp = Instant.now();
    }

    public Long getOrderId() { return orderId; }
    public String getAction() { return action; }
    public Instant getTimestamp() { return timestamp; }
}
