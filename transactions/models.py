from django.db import models
from django_extensions.db.fields import AutoSlugField

from store.models import Item
from accounts.models import Vendor, Customer
from django.db.models import Sum

DELIVERY_CHOICES = [("P", "Pending"), ("S", "Successful")]


class Sale(models.Model):
    """
    Represents a sale transaction involving a customer.
    """

    date_added = models.DateTimeField(
        auto_now_add=True,
        verbose_name="Sale Date"
    )
    customer = models.ForeignKey(
        Customer,
        on_delete=models.DO_NOTHING,
        db_column="customer"
    )
    sub_total = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0.0
    )
    grand_total = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0.0
    )
    tax_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0.0
    )
    tax_percentage = models.FloatField(default=0.0)
    amount_paid = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0.0
    )
    amount_change = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0.0
    )

    class Meta:
        db_table = "sales"
        verbose_name = "Sale"
        verbose_name_plural = "Sales"

    def __str__(self):
        """
        Returns a string representation of the Sale instance.
        """
        return (
            f"Sale ID: {self.id} | "
            f"Grand Total: {self.grand_total} | "
            f"Date: {self.date_added}"
        )

    def sum_products(self):
        """
        Returns the total quantity of products in the sale.
        """
        return sum(detail.quantity for detail in self.saledetail_set.all())
    
    @property
    def product_images(self):
        """
            Retourne la liste des URLs d'images des items liés à cette vente.
            Utiliser cette propriété dans les templates : elle renvoie une liste de chaînes (URLs) -
            celles qui n'ont pas d'image sont simplement ignorées.
      """
        images = []
          # Utiliser select_related si possible depuis la view pour optimiser la requête.
        for detail in self.saledetail_set.select_related('item'):
            item = getattr(detail, 'item', None)
            if not item:
               continue
            img_field = getattr(item, 'image', None)
            if img_field and hasattr(img_field, 'url'):
               images.append(img_field.url)
        return images
    @property
    def total_quantity(self):
        """
        Retourne la quantité totale vendue (somme des SaleDetail.quantity) pour cette vente.
        Utilisable directement dans les templates : {{ sale.total_quantity }}.
        Si le queryset est annoté (voir view), la valeur annotée sera utilisée automatiquement.
        """
        # si la vente a été annotée (optimisation en view), utilise l'annotation
        if hasattr(self, 'total_quantity') and self.__dict__.get('total_quantity') is not None:
            # déjà annoté par le queryset -> renvoyer tel quel
            return int(self.__dict__['total_quantity'] or 0)

        # sinon, calculer via aggregate (sécurisé si utilisé isolément)
        agg = self.saledetail_set.aggregate(total=Sum('quantity'))
        return int(agg['total'] or 0)


class SaleDetail(models.Model):
    """
    Represents details of a specific sale, including item and quantity.
    """

    sale = models.ForeignKey(
        Sale,
        on_delete=models.CASCADE,
        db_column="sale",
        related_name="saledetail_set"
    )
    item = models.ForeignKey(
        Item,
        on_delete=models.DO_NOTHING,
        db_column="item"
    )
    price = models.DecimalField(
        max_digits=10,
        decimal_places=2
    )
    quantity = models.PositiveIntegerField()
    total_detail = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        db_table = "sale_details"
        verbose_name = "Sale Detail"
        verbose_name_plural = "Sale Details"

    def __str__(self):
        """
        Returns a string representation of the SaleDetail instance.
        """
        return (
            f"Detail ID: {self.id} | "
            f"Sale ID: {self.sale.id} | "
            f"Quantity: {self.quantity}"
        )


class Purchase(models.Model):
    """
    Represents a purchase of an item,
    including vendor details and delivery status.
    """

    slug = AutoSlugField(unique=True, populate_from="vendor")
    item = models.ForeignKey(Item, on_delete=models.CASCADE)
    description = models.TextField(max_length=300, blank=True, null=True)
    vendor = models.ForeignKey(
        Vendor, related_name="purchases", on_delete=models.CASCADE
    )
    order_date = models.DateTimeField(auto_now_add=True)
    delivery_date = models.DateTimeField(
        blank=True, null=True, verbose_name="Delivery Date"
    )
    quantity = models.PositiveIntegerField(default=0)
    delivery_status = models.CharField(
        choices=DELIVERY_CHOICES,
        max_length=1,
        default="P",
        verbose_name="Delivery Status",
    )
    price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0.0,
        verbose_name="Price per item (Ksh)",
    )
    total_value = models.DecimalField(max_digits=10, decimal_places=2)

    def save(self, *args, **kwargs):
        """
        Calculates the total value before saving the Purchase instance.
        """
        self.total_value = self.price * self.quantity
        super().save(*args, **kwargs)
        # Update the item quantity
        self.item.quantity += self.quantity
        self.item.save()

    def __str__(self):
        """
        Returns a string representation of the Purchase instance.
        """
        return str(self.item.name)

    class Meta:
        ordering = ["order_date"]
