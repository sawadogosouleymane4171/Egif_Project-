"""
Module: store.views

Django views for the store app.
- Dashboard shows revenue based on paid_amount when available.
- Recent sales are serialized to expose paid_amount/balance safely.
"""

import operator
from functools import reduce
from typing import Any, Dict, List
from decimal import Decimal

from django.templatetags.static import static
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.urls import reverse, reverse_lazy
from django.views.decorators.http import require_POST, require_http_methods
from django.views.decorators.csrf import csrf_exempt
from django.db.models import Q, Count, Sum, F
from django.db.models.functions import TruncMonth, Coalesce
from django.contrib.auth import get_user_model

from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin

from django.views.generic import DetailView, CreateView, UpdateView, DeleteView, ListView
from django.views.generic.edit import FormMixin

from django_tables2 import SingleTableView
import django_tables2 as tables
from django_tables2.export.views import ExportMixin

from accounts.models import Profile, Vendor
from transactions.models import Sale
from .models import Category, Item, Delivery
from .forms import ItemForm, CategoryForm, DeliveryForm
from .tables import ItemTable
from django.conf import settings

# Optional import for top-items aggregation
try:
    from transactions.models import SaleDetail
except Exception:
    SaleDetail = None


@login_required
def dashboard(request: HttpRequest) -> HttpResponse:
    """
    Dashboard:
    - Detects real payment field (amount_paid or paid_amount)
    - total_revenue aggregates on that field when available (true encaissements)
    - recent_sales serialized with paid_amount & balance_due for template compatibility
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
    # If a payment_field exists, aggregate on it (true revenue/encaissé).
    if payment_field:
        # Sum of the actual payment field across all sales (includes partial payments)
        total_revenue_val = Sale.objects.aggregate(total=Coalesce(Sum(payment_field), Decimal('0')))['total']

        # monthly series aggregated on the payment_field
        sales_by_month_qs = (
            Sale.objects
            .annotate(month=TruncMonth('date_added'))
            .values('month')
            .annotate(revenue=Coalesce(Sum(payment_field), Decimal('0')), count=Count('id'))
            .order_by('month')
        )
    else:
        # fallback: sum of grand_total but only on sales considered "paid"
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
    # build list of value keys to request from DB
    values_keys = ['id', 'date_added', 'grand_total', 'customer__id', 'customer__first_name', 'customer__last_name', 'customer__phone']
    if payment_field:
        values_keys.append(payment_field)
    # fetch recent sales (from paid_sales_qs to keep "recent sales" = paid sales)
    recent_qs = paid_sales_qs.order_by('-date_added')[:10]
    recent_sales_raw = list(recent_qs.values(*values_keys))

    recent_sales: List[Dict[str, Any]] = []
    for s in recent_sales_raw:
        # normalize paid_amount key for template (even if actual field is amount_paid)
        paid_val = None
        if payment_field:
            paid_val = s.get(payment_field) or Decimal('0')
        # ensure numeric float for template
        try:
            paid_val_num = float(paid_val) if paid_val is not None else 0.0
        except Exception:
            paid_val_num = 0.0

        # compute balance_due if possible (grand_total - paid)
        balance_val_num = None
        try:
            gt = s.get('grand_total') or Decimal('0')
            gt_num = float(gt) if isinstance(gt, Decimal) else (float(gt) if gt is not None else 0.0)
            balance_val_num = gt_num - paid_val_num
        except Exception:
            balance_val_num = None

        # customer label
        cust_name = None
        if s.get('customer__first_name') or s.get('customer__last_name'):
            cust_name = f"{(s.get('customer__first_name') or '').strip()} {(s.get('customer__last_name') or '').strip()}".strip()
        customer_label = cust_name or (s.get('customer__phone') and str(s.get('customer__phone'))) or f"Sale #{s.get('id')}"

        recent_sales.append({
            'id': s.get('id'),
            'date_added': s.get('date_added'),
            # keep original grand_total for reference
            'grand_total': float(s.get('grand_total')) if isinstance(s.get('grand_total'), Decimal) else (float(s.get('grand_total')) if s.get('grand_total') is not None else 0.0),
            # normalized keys expected by template
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
            # If SaleDetail has FK 'sale', limit to paid sales
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

    context = {
        'total_sales': total_sales,
        'paid_sales_count': paid_sales_count,
        'sales_count': total_sales,
        'total_revenue': total_revenue,  # revenue based on amount_paid/paid_amount when possible
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
            # If Item has description field, include it; otherwise search name only
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


# --- class-based views unchanged (kept as before) ---
# ... (rest of CBVs unchanged, omitted for brevity in this snippet)
# If you want the full file with CBVs included, use the previous full file you had and replace only the dashboard + get_items_ajax_view parts.


# --- Class-based views (unchanged) ---
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
