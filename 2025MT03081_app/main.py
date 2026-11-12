import os
from datetime import datetime, timezone
from typing import List, Optional
import httpx
from fastapi import FastAPI, HTTPException, Depends
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, Column, Integer, String, Float
from sqlalchemy.orm import declarative_base, sessionmaker, Session




# Configuration & Database Setup
# Uses Kubernetes DNS service name by default; falls back to localhost for local dev
INVENTORY_SERVICE_URL = os.getenv("INVENTORY_SERVICE_URL", "http://inventory-service:8001")

DATABASE_URL = "sqlite:///./orders.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()



class OrderModel(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True, index=True)
    customer_id = Column(String, nullable=False, index=True)
    product_id = Column(Integer, nullable=False)
    product_name = Column(String, nullable=False)
    quantity = Column(Integer, nullable=False)
    unit_price = Column(Float, nullable=False)
    total_price = Column(Float, nullable=False)
    status = Column(String, nullable=False)  # CONFIRMED or REJECTED
    created_at = Column(String, nullable=False)



Base.metadata.create_all(bind=engine)



# Pydantic Schemas (DTOs for Commands & Queries)
class PlaceOrderCommand(BaseModel):
    customer_id: str = Field(..., examples=["CUST-1001"])
    product_id: int = Field(..., examples=[1])
    quantity: int = Field(..., gt=0, examples=[2])



class OrderResponse(BaseModel):
    id: int
    customer_id: str
    product_id: int
    product_name: str
    quantity: int
    unit_price: float
    total_price: float
    status: str
    created_at: str

    class Config:
        from_attributes = True




# FastAPI Application & Dependency Injection
app = FastAPI(
    title="Order Processing Microservice",
    description="Handles customer order placement and collaborates with Catalog-Inventory-Service.",
    version="1.0.0",
)



def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()



@app.get("/health", tags=["Health"])
def health_check():
    return {
        "service": "order-service",
        "status": "UP",
        "inventory_endpoint": INVENTORY_SERVICE_URL,
    }



# COMMAND: placeOrder (Collaborates with Catalog-Inventory-Service over REST)
@app.post("/orders", response_model=OrderResponse, status_code=201, tags=["Commands"])
def place_order(payload: PlaceOrderCommand, db: Session = Depends(get_db)):
    reserve_url = f"{INVENTORY_SERVICE_URL}/products/{payload.product_id}/reserve"

    try:
        with httpx.Client(timeout=5.0) as client:
            response = client.put(reserve_url, json={"quantity": payload.quantity})
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Failed to communicate with Catalog-Inventory-Service at {INVENTORY_SERVICE_URL}: {str(exc)}",
        )

    if response.status_code != 200:
        error_detail = response.json().get("detail", "Stock reservation failed.")
        raise HTTPException(status_code=response.status_code, detail=error_detail)

    product_data = response.json()
    unit_price = product_data["price"]
    total_price = round(unit_price * payload.quantity, 2)

    new_order = OrderModel(
        customer_id=payload.customer_id,
        product_id=payload.product_id,
        product_name=product_data["name"],
        quantity=payload.quantity,
        unit_price=unit_price,
        total_price=total_price,
        status="CONFIRMED",
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    db.add(new_order)
    db.commit()
    db.refresh(new_order)
    return new_order



# QUERY: getOrders / getCustomerOrders
@app.get("/orders", response_model=List[OrderResponse], tags=["Queries"])
def get_orders(customer_id: Optional[str] = None, db: Session = Depends(get_db)):
    query = db.query(OrderModel)
    if customer_id:
        query = query.filter(OrderModel.customer_id == customer_id)
    return query.all()



# QUERY: getOrderById
@app.get("/orders/{order_id}", response_model=OrderResponse, tags=["Queries"])
def get_order_by_id(order_id: int, db: Session = Depends(get_db)):
    order = db.query(OrderModel).filter(OrderModel.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail=f"Order ID {order_id} not found.")
    return order