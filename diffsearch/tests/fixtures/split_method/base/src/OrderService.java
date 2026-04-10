package com.flowmart.orders.service;

@Service
public class OrderService {

    private final OrderRepository orderRepository;
    private final InventoryService inventoryService;
    private final PaymentService paymentService;
    private final NotificationService notificationService;

    public Order processOrder(Long orderId, PaymentInfo payment) {
        Order order = orderRepository.findById(orderId)
                .orElseThrow(() -> new OrderNotFoundException(orderId));
        if (order.getStatus() != OrderStatus.PENDING) {
            throw new IllegalStateException("Order not pending");
        }
        if (payment.getAmount().compareTo(order.getTotal()) < 0) {
            throw new InsufficientPaymentException(orderId);
        }
        order.setStatus(OrderStatus.PROCESSING);
        for (OrderItem item : order.getItems()) {
            inventoryService.reserveInventory(item);
        }
        paymentService.charge(payment);
        order.setStatus(OrderStatus.COMPLETED);
        orderRepository.save(order);
        notificationService.sendConfirmation(order);
        return order;
    }
}
