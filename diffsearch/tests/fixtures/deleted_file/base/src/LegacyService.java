package com.flowmart.orders.legacy;

/**
 * @deprecated Use OrderService instead.
 */
@Deprecated
public class LegacyService {

    private final OrderRepository orderRepository;

    public LegacyService(OrderRepository orderRepository) {
        this.orderRepository = orderRepository;
    }

    public Order findOrder(Long id) {
        return orderRepository.findById(id).orElse(null);
    }

    public void deleteOrder(Long id) {
        orderRepository.deleteById(id);
    }
}
