---
tools_add: [post_comment, set_review_status]
---
PR: {pr_title}
{pr_description}

Commits *(oldest → newest)*:

{commits}

Existing threads on this PR:

{existing_comments}

Investigators returned the findings below. Consolidate them
(merge duplicates, keep the higher severity, drop anything already
covered in an open thread), publish each via post_comment(file,
line, severity, text), set the verdict via set_review_status, and
finish with done(findings).

FINDINGS:

  [BLOCKER] src/main/java/com/flowmart/orders/service/PricingService.java:95
  selectFreeItem returns get(0) — picks the first item, not the cheapest.
  Per AGENTS.md the buy-N-get-1-free promotion must give the customer the
  cheapest item free, not the first one in the qualifying group. Current
  implementation returns `group.get(0)` and silently overcharges customers
  whose cart contains items at different price points.
  Evidence: PricingService.java:95 `return group.get(0);`. AGENTS.md:
  "the free item is always the cheapest eligible item in the qualifying
  group — not the first, not the most expensive."

  [MAJOR] src/main/java/com/flowmart/orders/service/PricingService.java:78
  applyBulkDiscount is missing @Transactional. The method writes to
  orderItemRepository and then orderRepository without a surrounding
  transaction; partial failures leave items discounted but order totals
  stale. Every other multi-write method in the codebase is @Transactional
  (OrderService.cancelOrder, .placeOrder, .updateStatus).

  [MINOR] src/main/java/com/flowmart/orders/model/Promotion.java:12
  Promotion entity uses manual getters/setters instead of Lombok
  (@Data @Builder @NoArgsConstructor @AllArgsConstructor). Every other
  entity in com.flowmart.orders.model uses Lombok; Promotion is the odd
  one out.
