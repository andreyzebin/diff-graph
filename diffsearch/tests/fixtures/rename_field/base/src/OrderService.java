package com.flowmart.orders.service;

import org.springframework.stereotype.Service;

@Service
public class OrderService {

    private final OrderRepository orderRepository;
    private final InventoryClient inventoryClient;

    public OrderService(OrderRepository orderRepository, InventoryClient inventoryClient) {
        this.orderRepository = orderRepository;
        this.inventoryClient = inventoryClient;
    }

    public Order createOrder(OrderRequest req) {
        Order order = Order.builder()
                .items(req.getItems())
                .build();
        return orderRepository.save(order);
    }

    public void cancelOrder(Long orderId) {
        Order order = orderRepository.findById(orderId)
                .orElseThrow(RuntimeException::new);
        for (OrderItem item : order.getItems()) {
            inventoryClient.release(item);
        }
        order.setStatus(OrderStatus.CANCELLED);
        orderRepository.save(order);
    }
}
