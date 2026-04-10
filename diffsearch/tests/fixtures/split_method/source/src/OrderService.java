package com.flowmart.orders.service;

@Service
public class OrderService {

    private final OrderRepository orderRepository;
    private final InventoryService inventoryService;
    private final PaymentService paymentService;
    private final NotificationService notificationService;
    private final AuditRepository auditRepository;

    public void validateOrder(Long orderId, PaymentInfo payment) {
        Order order = orderRepository.findById(orderId)
                .orElseThrow(() -> new OrderNotFoundException(orderId));
        if (order.getStatus() != OrderStatus.PENDING) {
            throw new IllegalStateException("Order not pending");
        }
        if (payment.getAmount().compareTo(order.getTotal()) < 0) {
            throw new InsufficientPaymentException(orderId);
        }
    }

    public Order executeOrder(Long orderId, PaymentInfo payment) {
        Order order = orderRepository.findById(orderId)
                .orElseThrow(() -> new OrderNotFoundException(orderId));
        order.setStatus(OrderStatus.PROCESSING);
        for (OrderItem item : order.getItems()) {
            inventoryService.reserveInventory(item);
        }
        paymentService.charge(payment);
        order.setStatus(OrderStatus.COMPLETED);
        orderRepository.save(order);
        auditLog(order, "executed");
        notificationService.sendConfirmation(order);
        return order;
    }

    private void auditLog(Order order, String action) {
        log.info("Order {} action={} status={}", order.getId(), action, order.getStatus());
        auditRepository.save(new AuditEntry(order.getId(), action, Instant.now()));
    }
}
