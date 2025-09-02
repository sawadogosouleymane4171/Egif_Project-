"""
Module: store.views

Contains Django views for managing items, profiles,
and deliveries in the store application.

Classes handle product listing, creation, updating,
deletion, and delivery management.
The module integrates with Django's authentication
and querying functionalities.
"""

# Standard library imports
import operator
from functools import reduce
from django.templatetags.static import static

from typing import Any, Dict, List
from decimal import Decimal
from django.http import HttpRequest, HttpResponse


# Django core imports
from django.shortcuts import render
from django.urls import reverse, reverse_lazy
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.views.decorators.csrf import csrf_exempt
from django.db.models import Q, Count, Sum
from django.db.models.functions import TruncMonth
from django.contrib.auth import get_user_model

# Authentication and permissions
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin

# Class-based views
from django.views.generic import (
    DetailView, CreateView, UpdateView, DeleteView, ListView
)
from django.views.generic.edit import FormMixin

# Third-party packages
from django_tables2 import SingleTableView
import django_tables2 as tables
from django_tables2.export.views import ExportMixin

# Local app imports
from accounts.models import Profile, Vendor
from transactions.models import Sale
from .models import Category, Item, Delivery
from .forms import ItemForm, CategoryForm, DeliveryForm
from .tables import ItemTable
from django.conf import settings
from django.views.decorators.http import require_http_methods
from django.db.models.functions import TruncMonth, Coalesce



# Si SaleDetail est défini dans transactions.models, on l'importe pour récupérer top items
try:
    from transactions.models import Sale, SaleDetail
except Exception:
    # Si SaleDetail n'existe pas, on importe au moins Sale pour le dashboard
    from transactions.models import Sale
    SaleDetail = None


@login_required
def dashboard(request: HttpRequest) -> HttpResponse:
    """
    Dashboard optimisé :
    - réduit les requêtes en utilisant `aggregate` / `annotate` avec `filter` lorsque possible
    - normalise recent_deliveries en dicts simples
    - conversions sûres de Decimal -> float pour templates/JS
    """
    # ---- Sales aggregates ----
    total_sales = Sale.objects.count()
    total_revenue = Sale.objects.aggregate(total=Coalesce(Sum('grand_total'), Decimal('0')))['total']
    # convert Decimal -> float (utile pour JSON/chart libs). Garde prudence si tu veux précision financière.
    total_revenue = float(total_revenue) if isinstance(total_revenue, Decimal) else total_revenue

    # ---- Items / stock ----
    total_products = Item.objects.count()
    low_stock_threshold = getattr(settings, 'LOW_STOCK_THRESHOLD', 5)
    low_stock_products = Item.objects.filter(quantity__lte=low_stock_threshold)
    low_stock_count = low_stock_products.count()

    # ---- Deliveries aggregates (single DB hit pour les statuts) ----
    deliveries_total = Delivery.objects.count()
    deliveries_status_agg = Delivery.objects.aggregate(
        delivered=Count('pk', filter=Q(is_delivered=True)),
        pending=Count('pk', filter=Q(is_delivered=False))
    )
    deliveries_by_status = {
        'Delivered': deliveries_status_agg.get('delivered', 0) or 0,
        'Pending': deliveries_status_agg.get('pending', 0) or 0,
    }

    # ---- Recent deliveries (sélection avec values() pour éviter chargement d'objets complets) ----
    recent_deliveries_qs = Delivery.objects.order_by('-date').values(
        'id', 'customer_name', 'phone_number', 'date', 'is_delivered', 'location'
    )[:10]

    recent_deliveries: List[Dict[str, Any]] = []
    for d in recent_deliveries_qs:
        # customer_label : priorise customer_name, puis phone, sinon fallback
        customer_label = d.get('customer_name') or (d.get('phone_number') and str(d.get('phone_number'))) or f"Delivery #{d['id']}"
        recent_deliveries.append({
            'id': d['id'],
            'customer_label': customer_label,
            'date': d.get('date'),
            'status_label': 'Delivered' if d.get('is_delivered') else 'Pending',
            'location': d.get('location') or '',
            'phone_number': str(d.get('phone_number')) if d.get('phone_number') else ''
        })

    # ---- Sales by month (liste prête pour les graphiques) ----
    sales_by_month_qs = (
        Sale.objects
        .annotate(month=TruncMonth('date_added'))
        .values('month')
        .annotate(revenue=Coalesce(Sum('grand_total'), Decimal('0')), count=Count('id'))
        .order_by('month')
    )

    sales_dates = [entry['month'].strftime('%Y-%m') for entry in sales_by_month_qs]
    sales_values = [float(entry['revenue']) for entry in sales_by_month_qs]
    sales_counts = [int(entry['count']) for entry in sales_by_month_qs]

    # ---- Deliveries by month ----
    deliveries_by_month_qs = (
        Delivery.objects
        .annotate(month=TruncMonth('date'))
        .values('month')
        .annotate(count=Count('id'))
        .order_by('month')
    )
    delivery_months = [entry['month'].strftime('%Y-%m') for entry in deliveries_by_month_qs]
    delivery_counts = [int(entry['count']) for entry in deliveries_by_month_qs]

    # ---- Recent sales & top items ----
    recent_sales = Sale.objects.order_by('-date_added')[:10]  # si tu veux sérialiser, utilise .values(...)

    top_items = []
    # Vérifier si SaleDetail existe (sécurité si modèle optionnel)
    try:
        # simple aggregation pour les top items
        top_qs = (
            SaleDetail.objects
            .values('item__id', 'item__name')
            .annotate(total_qty=Coalesce(Sum('quantity'), 0))
            .order_by('-total_qty')[:10]
        )
        top_items = [
            {'id': e['item__id'], 'name': e['item__name'], 'qty': int(e['total_qty'])}
            for e in top_qs
        ]
    except NameError:
        # SaleDetail non défini : on ignore proprement
        top_items = []

    # ---- Profiles (staff users) ----
    profiles_count = get_user_model().objects.filter(is_staff=True).count()

    context = {
        'total_sales': total_sales,
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
    }
    return render(request, 'store/dashboard.html', context)

@require_http_methods(["GET", "POST"])

def get_items_ajax_view(request):
    """
    Retourne JSON pour Select2. Accepte GET et POST.
    Renvoie toujours {'results': [...] } ou [] en cas d'erreur.
    """
    try:
        term = request.GET.get('term', '').strip() if request.method == 'GET' else request.POST.get('term', '').strip()

        qs = Item.objects.all()
        if term:
            qs = qs.filter(Q(name__icontains=term) |
                           Q(description__icontains=term) if hasattr(Item, 'description') else Q(name__icontains=term))
        qs = qs[:20]

        data = []
        for item in qs:
            # safe image URL
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
        # renvoyer un message d'erreur lisible pour debugging
        return JsonResponse({'results': [], 'error': str(e)}, status=500)

class ProductListView(LoginRequiredMixin, ExportMixin, tables.SingleTableView):
    """
    View class to display a list of products.

    Attributes:
    - model: The model associated with the view.
    - table_class: The table class used for rendering.
    - template_name: The HTML template used for rendering the view.
    - context_object_name: The variable name for the context object.
    - paginate_by: Number of items per page for pagination.
    """

    model = Item
    table_class = ItemTable
    template_name = "store/productslist.html"
    context_object_name = "items"
    paginate_by = 10
    SingleTableView.table_pagination = False


class ItemSearchListView(ProductListView):
    """
    View class to search and display a filtered list of items.

    Attributes:
    - paginate_by: Number of items per page for pagination.
    """

    paginate_by = 10

    def get_queryset(self):
        result = super(ItemSearchListView, self).get_queryset()

        query = self.request.GET.get("q")
        if query:
            query_list = query.split()
            result = result.filter(
                reduce(
                    operator.and_, (Q(name__icontains=q) for q in query_list)
                )
            )
        return result


class ProductDetailView(LoginRequiredMixin, DetailView):
    """
    View class to display detailed information about a product.

    Attributes:
    - model: The model associated with the view.
    - template_name: The HTML template used for rendering the view.
    """

    model = Item
    template_name = "store/productdetail.html"

    def get_success_url(self):
        return reverse("product-detail", kwargs={"slug": self.object.slug})


class ProductCreateView(LoginRequiredMixin, CreateView):
    """
    View class to create a new product.

    Attributes:
    - model: The model associated with the view.
    - template_name: The HTML template used for rendering the view.
    - form_class: The form class used for data input.
    - success_url: The URL to redirect to upon successful form submission.
    """

    model = Item
    template_name = "store/productcreate.html"
    form_class = ItemForm
    success_url = "/products"

    def test_func(self):
        # item = Item.objects.get(id=pk)
        if self.request.POST.get("quantity") < 1:
            return False
        else:
            return True


class ProductUpdateView(LoginRequiredMixin, UserPassesTestMixin, UpdateView):
    """
    View class to update product information.

    Attributes:
    - model: The model associated with the view.
    - template_name: The HTML template used for rendering the view.
    - fields: The fields to be updated.
    - success_url: The URL to redirect to upon successful form submission.
    """

    model = Item
    template_name = "store/productupdate.html"
    form_class = ItemForm
    success_url = "/products"

    def test_func(self):
        if self.request.user.is_superuser:
            return True
        else:
            return False


class ProductDeleteView(LoginRequiredMixin, UserPassesTestMixin, DeleteView):
    """
    View class to delete a product.

    Attributes:
    - model: The model associated with the view.
    - template_name: The HTML template used for rendering the view.
    - success_url: The URL to redirect to upon successful deletion.
    """

    model = Item
    template_name = "store/productdelete.html"
    success_url = "/products"

    def test_func(self):
        if self.request.user.is_superuser:
            return True
        else:
            return False


class DeliveryListView(
    LoginRequiredMixin, ExportMixin, tables.SingleTableView
):
    """
    View class to display a list of deliveries.

    Attributes:
    - model: The model associated with the view.
    - pagination: Number of items per page for pagination.
    - template_name: The HTML template used for rendering the view.
    - context_object_name: The variable name for the context object.
    """

    model = Delivery
    pagination = 10
    template_name = "store/deliveries.html"
    context_object_name = "deliveries"


class DeliverySearchListView(DeliveryListView):
    """
    View class to search and display a filtered list of deliveries.

    Attributes:
    - paginate_by: Number of items per page for pagination.
    """

    paginate_by = 10

    def get_queryset(self):
        result = super(DeliverySearchListView, self).get_queryset()

        query = self.request.GET.get("q")
        if query:
            query_list = query.split()
            result = result.filter(
                reduce(
                    operator.
                    and_, (Q(customer_name__icontains=q) for q in query_list)
                )
            )
        return result


class DeliveryDetailView(LoginRequiredMixin, DetailView):
    """
    View class to display detailed information about a delivery.

    Attributes:
    - model: The model associated with the view.
    - template_name: The HTML template used for rendering the view.
    """

    model = Delivery
    template_name = "store/deliverydetail.html"


class DeliveryCreateView(LoginRequiredMixin, CreateView):
    """
    View class to create a new delivery.

    Attributes:
    - model: The model associated with the view.
    - fields: The fields to be included in the form.
    - template_name: The HTML template used for rendering the view.
    - success_url: The URL to redirect to upon successful form submission.
    """

    model = Delivery
    form_class = DeliveryForm
    template_name = "store/delivery_form.html"
    success_url = "/deliveries"


class DeliveryUpdateView(LoginRequiredMixin, UpdateView):
    """
    View class to update delivery information.

    Attributes:
    - model: The model associated with the view.
    - fields: The fields to be updated.
    - template_name: The HTML template used for rendering the view.
    - success_url: The URL to redirect to upon successful form submission.
    """

    model = Delivery
    form_class = DeliveryForm
    template_name = "store/delivery_form.html"
    success_url = "/deliveries"


class DeliveryDeleteView(LoginRequiredMixin, UserPassesTestMixin, DeleteView):
    """
    View class to delete a delivery.

    Attributes:
    - model: The model associated with the view.
    - template_name: The HTML template used for rendering the view.
    - success_url: The URL to redirect to upon successful deletion.
    """

    model = Delivery
    template_name = "store/productdelete.html"
    success_url = "/deliveries"

    def test_func(self):
        if self.request.user.is_superuser:
            return True
        else:
            return False


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


@csrf_exempt
@require_POST
@login_required
def get_items_ajax_view(request):
    if is_ajax(request):
        try:
            term = request.POST.get("term", "")
            data = []

            items = Item.objects.filter(name__icontains=term)
            for item in items[:10]:
                data.append(item.to_json())

            return JsonResponse(data, safe=False)
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=500)
    return JsonResponse({'error': 'Not an AJAX request'}, status=400)
def get_items_ajax_view(request):
    if is_ajax(request):
        try:
            term = request.POST.get("term", "")
            data = []

            items = Item.objects.filter(name__icontains=term)
            for item in items[:10]:
                data.append(item.to_json())

            return JsonResponse(data, safe=False)
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=500)
    return JsonResponse({'error': 'Not an AJAX request'}, status=400)
