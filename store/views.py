"""
Module: store.views

Vue mise à jour du dashboard pour inclure le coût total d'inventaire
et une endpoint AJAX pour supprimer un item et renvoyer le nouveau coût.

Contenu:
- dashboard(request): calcul du total_revenue (logique existante) + total_inventory_cost
- get_items_ajax_view(request): autocomplete Select2 (renvoie {'results': [...]})
- delete_item_ajax(request): suppression sécurisée d'un Item et recalcul du coût
- util: compute_total_inventory_cost() pour centraliser la logique

Remarque: n'oublie pas d'ajouter l'URL pour delete_item_ajax dans urls.py, et
côté template dashboard.html, placer un élément avec id "total_inventory_cost"
pour que le JS puisse mettre à jour la valeur après suppression.
"""

import operator
from functools import reduce
from typing import Any, Dict, List, Optional
from decimal import Decimal

from django.templatetags.static import static
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render, get_object_or_404
from django.urls import reverse, reverse_lazy
from django.views.decorators.http import require_POST, require_http_methods
from django.views.decorators.csrf import csrf_exempt
from django.db import transaction
from django.db.models import Q, Count, Sum, F, ExpressionWrapper
from django.db.models.functions import TruncMonth, Coalesce
from django.db.models import DecimalField
from django.contrib.auth import get_user_model

from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin

from django.views.generic import DetailView, CreateView, UpdateView, DeleteView, ListView
from django.views.generic.edit import FormMixin

from django_tables2 import SingleTableView
import django_tables2 as tables
from django_tables2.export.views import ExportMixin

from accounts.models import Profile, Vendor
from transactions.models import Sale, Purchase  # Ajout pour accès aux achats
from .models import Category, Item, Delivery
from .forms import ItemForm, CategoryForm, DeliveryForm
from .tables import ItemTable
from django.conf import settings

# Optional import for top-items aggregation
try:
    from transactions.models import SaleDetail
except Exception:
    SaleDetail = None


# -------------------- Utilities --------------------
def compute_total_inventory_cost() -> Decimal:
    """Compute the total inventory cost as SUM(quantity * unit_cost)

    The function tries to detect a sensible "cost" field on Item using a
    list of common field names. If none is found it falls back to 'price'.
    Returns a Decimal (0 if not computable).
    """
    cost_field_candidates = ['cost_price', 'purchase_price', 'cost', 'unit_cost', 'buy_price', 'purchase_cost']
    try:
        item_field_names = [f.name for f in Item._meta.get_fields()]
    except Exception:
        item_field_names = []

    chosen_field: Optional[str] = None
    for cand in cost_field_candidates:
        if cand in item_field_names:
            chosen_field = cand
            break

    if not chosen_field and 'price' in item_field_names:
        chosen_field = 'price'

    if not chosen_field:
        return Decimal('0')

    # Build expression: quantity * chosen_field
    expr = ExpressionWrapper(
        F('quantity') * F(chosen_field),
        output_field=DecimalField(max_digits=20, decimal_places=2)
    )

    total_val = Item.objects.aggregate(total=Coalesce(Sum(expr), Decimal('0')))['total'] or Decimal('0')
    return total_val


def compute_total_purchase_cost() -> Decimal:
    """
    Calcule le coût total d'achat de tous les produits (somme des achats).
    """
    try:
        total = Purchase.objects.aggregate(total=Coalesce(Sum('total_value'), Decimal('0')))['total']
        return total or Decimal('0')
    except Exception:
        return Decimal('0')


# -------------------- Views --------------------

@login_required
def dashboard(request: HttpRequest) -> HttpResponse:
    """
    Dashboard:
    - Detects real payment field (amount_paid or paid_amount)
    - total_revenue aggregates on that field when available (true encaissements)
    - recent_sales serialized with paid_amount & balance_due for template compatibility
    - compute total inventory cost and expose it to the template
    """
    # ---- Detect Sale fields ----
    try:
        sale_field_names = [f.name for f in Sale._meta.get_fields()]
    except Exception:
        sale_field_names = []

    # determine the payment field present on Sale
    payment_field = None
    if 'amount_paid' in sale_field_names:
        payment_field = 'amount_paid'
    elif 'paid_amount' in sale_field_names:
        payment_field = 'paid_amount'
    else:
        payment_field = None  # no explicit payment field

    # Build 'paid_sales_qs' using common conventions (sales considered "paid")
    if 'is_paid' in sale_field_names:
        paid_sales_qs = Sale.objects.filter(is_paid=True)
    elif 'is_fully_paid' in sale_field_names:
        paid_sales_qs = Sale.objects.filter(is_fully_paid=True)
    elif 'payment_status' in sale_field_names:
        paid_sales_qs = Sale.objects.filter(payment_status__in=['paid', 'PAID', 'completed', 'COMPLETED'])
    elif payment_field and 'grand_total' in sale_field_names:
        # consider sale "paid" if paid_amount/amount_paid >= grand_total
        paid_sales_qs = Sale.objects.filter(**{f"{payment_field}__gte": F('grand_total')})
    elif 'balance_due' in sale_field_names:
        paid_sales_qs = Sale.objects.filter(balance_due__lte=0)
    else:
        paid_sales_qs = Sale.objects.all()

    # ---- TOTAL REVENUE and monthly series ----
    if payment_field:
        total_revenue_val = Sale.objects.aggregate(total=Coalesce(Sum(payment_field), Decimal('0')))['total']
        sales_by_month_qs = (
            Sale.objects
            .annotate(month=TruncMonth('date_added'))
            .values('month')
            .annotate(revenue=Coalesce(Sum(payment_field), Decimal('0')), count=Count('id'))
            .order_by('month')
        )
    else:
        total_revenue_val = paid_sales_qs.aggregate(total=Coalesce(Sum('grand_total'), Decimal('0')))['total']
        sales_by_month_qs = (
            paid_sales_qs
            .annotate(month=TruncMonth('date_added'))
            .values('month')
            .annotate(revenue=Coalesce(Sum('grand_total'), Decimal('0')), count=Count('id'))
            .order_by('month')
        )

    total_revenue = float(total_revenue_val) if isinstance(total_revenue_val, Decimal) else total_revenue_val

    # ---- Basic counts / stock ----
    total_sales = Sale.objects.count()
    paid_sales_count = paid_sales_qs.count()
    total_products = Item.objects.count()
    low_stock_threshold = getattr(settings, 'LOW_STOCK_THRESHOLD', 5)
    low_stock_products = Item.objects.filter(quantity__lte=low_stock_threshold)
    low_stock_count = low_stock_products.count()

    # ---- Deliveries aggregates ----
    deliveries_total = Delivery.objects.count()
    deliveries_status_agg = Delivery.objects.aggregate(
        delivered=Count('pk', filter=Q(is_delivered=True)),
        pending=Count('pk', filter=Q(is_delivered=False))
    )
    deliveries_by_status = {
        'Delivered': deliveries_status_agg.get('delivered', 0) or 0,
        'Pending': deliveries_status_agg.get('pending', 0) or 0,
    }

    # ---- Recent deliveries serialized ----
    recent_deliveries_qs = Delivery.objects.order_by('-date').values(
        'id', 'customer_name', 'phone_number', 'date', 'is_delivered', 'location'
    )[:10]

    recent_deliveries: List[Dict[str, Any]] = []
    for d in recent_deliveries_qs:
        customer_label = d.get('customer_name') or (d.get('phone_number') and str(d.get('phone_number'))) or f"Delivery #{d['id']}"
        recent_deliveries.append({
            'id': d['id'],
            'customer_label': customer_label,
            'date': d.get('date'),
            'status_label': 'Delivered' if d.get('is_delivered') else 'Pending',
            'location': d.get('location') or '',
            'phone_number': str(d.get('phone_number')) if d.get('phone_number') else ''
        })

    # ---- Sales by month series -> lists for JS charts ----
    sales_dates = [entry['month'].strftime('%Y-%m') for entry in sales_by_month_qs]
    sales_values = [float(entry['revenue']) for entry in sales_by_month_qs]
    sales_counts = [int(entry['count']) for entry in sales_by_month_qs]

    # ---- Deliveries by month series ----
    deliveries_by_month_qs = (
        Delivery.objects
        .annotate(month=TruncMonth('date'))
        .values('month')
        .annotate(count=Count('id'))
        .order_by('month')
    )
    delivery_months = [entry['month'].strftime('%Y-%m') for entry in deliveries_by_month_qs]
    delivery_counts = [int(entry['count']) for entry in deliveries_by_month_qs]

    # ---- Recent sales: serialize with unified keys (paid_amount & balance_due) for template compatibility ----
    values_keys = ['id', 'date_added', 'grand_total', 'customer__id', 'customer__first_name', 'customer__last_name', 'customer__phone']
    if payment_field:
        values_keys.append(payment_field)
    recent_qs = paid_sales_qs.order_by('-date_added')[:10]
    recent_sales_raw = list(recent_qs.values(*values_keys))

    recent_sales: List[Dict[str, Any]] = []
    for s in recent_sales_raw:
        paid_val = None
        if payment_field:
            paid_val = s.get(payment_field) or Decimal('0')
        try:
            paid_val_num = float(paid_val) if paid_val is not None else 0.0
        except Exception:
            paid_val_num = 0.0

        balance_val_num = None
        try:
            gt = s.get('grand_total') or Decimal('0')
            gt_num = float(gt) if isinstance(gt, Decimal) else (float(gt) if gt is not None else 0.0)
            balance_val_num = gt_num - paid_val_num
        except Exception:
            balance_val_num = None

        cust_name = None
        if s.get('customer__first_name') or s.get('customer__last_name'):
            cust_name = f"{(s.get('customer__first_name') or '').strip()} {(s.get('customer__last_name') or '').strip()}".strip()
        customer_label = cust_name or (s.get('customer__phone') and str(s.get('customer__phone'))) or f"Sale #{s.get('id')}"

        recent_sales.append({
            'id': s.get('id'),
            'date_added': s.get('date_added'),
            'grand_total': float(s.get('grand_total')) if isinstance(s.get('grand_total'), Decimal) else (float(s.get('grand_total')) if s.get('grand_total') is not None else 0.0),
            'paid_amount': paid_val_num,
            'balance_due': balance_val_num,
            'customer__phone': s.get('customer__phone'),
            'customer_label': customer_label
        })

    # ---- Top items based on SaleDetail when available (and preferring paid sales) ----
    top_items = []
    try:
        if SaleDetail is not None:
            qs = SaleDetail.objects.all()
            if 'sale' in [f.name for f in SaleDetail._meta.get_fields()]:
                qs = qs.filter(sale__in=paid_sales_qs)
            top_qs = (
                qs
                .values('item__id', 'item__name')
                .annotate(total_qty=Coalesce(Sum('quantity'), 0))
                .order_by('-total_qty')[:10]
            )
            top_items = [
                {'id': e['item__id'], 'name': e['item__name'], 'qty': int(e['total_qty'])}
                for e in top_qs
            ]
    except Exception:
        top_items = []

    # ---- Profiles count ----
    profiles_count = get_user_model().objects.filter(is_staff=True).count()

    # ---- Inventory cost ----
    total_inventory_cost_val = compute_total_inventory_cost()
    total_inventory_cost = float(total_inventory_cost_val) if isinstance(total_inventory_cost_val, Decimal) else total_inventory_cost_val

    # ---- Purchase cost ----
    total_purchase_cost_val = compute_total_purchase_cost()
    total_purchase_cost = float(total_purchase_cost_val) if isinstance(total_purchase_cost_val, Decimal) else total_purchase_cost_val

    # ---- Context for template ----n    
    context = {
        'total_sales': total_sales,
        'paid_sales_count': paid_sales_count,
        'sales_count': total_sales,
        'total_revenue': total_revenue,
        'total_products': total_products,
        'total_items': total_products,
        'profiles_count': profiles_count,
        'delivery_count': deliveries_total,
        'deliveries_total': deliveries_total,
        'deliveries_by_status': deliveries_by_status,
        'recent_deliveries': recent_deliveries,
        'low_stock_count': low_stock_count,
        'low_stock_products': low_stock_products,
        'sales_dates': sales_dates,
        'sales_values': sales_values,
        'sales_counts': sales_counts,
        'delivery_months': delivery_months,
        'delivery_counts': delivery_counts,
        'recent_sales': recent_sales,
        'top_items': top_items,
        'currency': getattr(settings, 'CURRENCY', 'FCFA'),
        'low_stock_threshold': low_stock_threshold,
        # Inventory cost
        'total_inventory_cost': total_inventory_cost,
        'total_purchase_cost': total_purchase_cost,  # Ajout pour le template
        'inventory_cost_field': None,
    }
    return render(request, 'store/dashboard.html', context)


@require_http_methods(["GET", "POST"])
def get_items_ajax_view(request):
    """
    Robust get-items for Select2 / autocomplete.
    Accepts GET (term param) or POST.
    Returns {'results': [...]} or error message.
    """
    try:
        term = request.GET.get('term', '').strip() if request.method == 'GET' else request.POST.get('term', '').strip()
        qs = Item.objects.all()
        if term:
            if hasattr(Item, 'description'):
                qs = qs.filter(Q(name__icontains=term) | Q(description__icontains=term))
            else:
                qs = qs.filter(name__icontains=term)

        qs = qs[:20]
        data = []
        for item in qs:
            image_url = ''
            try:
                if getattr(item, 'image', None) and hasattr(item.image, 'url'):
                    image_url = request.build_absolute_uri(item.image.url)
            except Exception:
                image_url = ''
            if not image_url:
                image_url = request.build_absolute_uri(static('images/placeholder.png'))

            data.append({
                'id': item.pk,
                'text': item.name,
                'name': item.name,
                'price': float(getattr(item, 'price', 0) or 0),
                'quantity': int(getattr(item, 'quantity', 0) or 0),
                'image': image_url
            })

        return JsonResponse({'results': data}, safe=False)
    except Exception as e:
        return JsonResponse({'results': [], 'error': str(e)}, status=500)


@require_POST
def delete_item_ajax(request):
    """
    Supprime un Item (par id) et renvoie le nouveau coût total de l'inventaire.
    Permissions: staff required by default (adapte à ton besoin).
    """
    if not request.user.is_authenticated or not request.user.is_staff:
        return JsonResponse({'success': False, 'error': 'Permission denied'}, status=403)

    item_id = request.POST.get('id')
    if not item_id:
        return JsonResponse({'success': False, 'error': 'Missing item id'}, status=400)

    try:
        with transaction.atomic():
            item = get_object_or_404(Item, pk=item_id)
            item.delete()
            new_total = compute_total_inventory_cost()
            new_total_val = float(new_total) if isinstance(new_total, Decimal) else new_total

        return JsonResponse({'success': True, 'total_inventory_cost': new_total_val})
    except Item.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Item not found'}, status=404)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


# --- class-based views unchanged (kept as before) ---
# ... (rest of CBVs unchanged, omitted for brevity in this snippet)


class ProductListView(LoginRequiredMixin, ExportMixin, tables.SingleTableView):
    model = Item
    table_class = ItemTable
    template_name = "store/productslist.html"
    context_object_name = "items"
    paginate_by = 10
    SingleTableView.table_pagination = False


class ItemSearchListView(ProductListView):
    paginate_by = 10

    def get_queryset(self):
        result = super(ItemSearchListView, self).get_queryset()
        query = self.request.GET.get("q")
        if query:
            query_list = query.split()
            result = result.filter(
                reduce(operator.and_, (Q(name__icontains=q) for q in query_list))
            )
        return result


class ProductDetailView(LoginRequiredMixin, DetailView):
    model = Item
    template_name = "store/productdetail.html"

    def get_success_url(self):
        return reverse("product-detail", kwargs={"slug": self.object.slug})


class ProductCreateView(LoginRequiredMixin, CreateView):
    model = Item
    template_name = "store/productcreate.html"
    form_class = ItemForm
    success_url = "/products"

    def test_func(self):
        if self.request.POST.get("quantity") < 1:
            return False
        return True


class ProductUpdateView(LoginRequiredMixin, UserPassesTestMixin, UpdateView):
    model = Item
    template_name = "store/productupdate.html"
    form_class = ItemForm
    success_url = "/products"

    def test_func(self):
        return self.request.user.is_superuser


class ProductDeleteView(LoginRequiredMixin, UserPassesTestMixin, DeleteView):
    model = Item
    template_name = "store/productdelete.html"
    success_url = "/products"

    def test_func(self):
        return self.request.user.is_superuser


class DeliveryListView(LoginRequiredMixin, ExportMixin, tables.SingleTableView):
    model = Delivery
    pagination = 10
    template_name = "store/deliveries.html"
    context_object_name = "deliveries"


class DeliverySearchListView(DeliveryListView):
    paginate_by = 10

    def get_queryset(self):
        result = super(DeliverySearchListView, self).get_queryset()
        query = self.request.GET.get("q")
        if query:
            query_list = query.split()
            result = result.filter(
                reduce(operator.and_, (Q(customer_name__icontains=q) for q in query_list))
            )
        return result


class DeliveryDetailView(LoginRequiredMixin, DetailView):
    model = Delivery
    template_name = "store/deliverydetail.html"


class DeliveryCreateView(LoginRequiredMixin, CreateView):
    model = Delivery
    form_class = DeliveryForm
    template_name = "store/delivery_form.html"
    success_url = "/deliveries"


class DeliveryUpdateView(LoginRequiredMixin, UpdateView):
    model = Delivery
    form_class = DeliveryForm
    template_name = "store/delivery_form.html"
    success_url = "/deliveries"


class DeliveryDeleteView(LoginRequiredMixin, UserPassesTestMixin, DeleteView):
    model = Delivery
    template_name = "store/productdelete.html"
    success_url = "/deliveries"

    def test_func(self):
        return self.request.user.is_superuser


class CategoryListView(LoginRequiredMixin, ListView):
    model = Category
    template_name = 'store/category_list.html'
    context_object_name = 'categories'
    paginate_by = 10
    login_url = 'login'


class CategoryDetailView(LoginRequiredMixin, DetailView):
    model = Category
    template_name = 'store/category_detail.html'
    context_object_name = 'category'
    login_url = 'login'


class CategoryCreateView(LoginRequiredMixin, CreateView):
    model = Category
    template_name = 'store/category_form.html'
    form_class = CategoryForm
    login_url = 'login'

    def get_success_url(self):
        return reverse_lazy('category-detail', kwargs={'pk': self.object.pk})


class CategoryUpdateView(LoginRequiredMixin, UpdateView):
    model = Category
    template_name = 'store/category_form.html'
    form_class = CategoryForm
    login_url = 'login'

    def get_success_url(self):
        return reverse_lazy('category-detail', kwargs={'pk': self.object.pk})


class CategoryDeleteView(LoginRequiredMixin, DeleteView):
    model = Category
    template_name = 'store/category_confirm_delete.html'
    context_object_name = 'category'
    success_url = reverse_lazy('category-list')
    login_url = 'login'


def is_ajax(request):
    return request.META.get('HTTP_X_REQUESTED_WITH') == 'XMLHttpRequest'
