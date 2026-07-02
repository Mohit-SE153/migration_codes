-- =====================================================================
-- Synthetic pilot-scale sample environment: "SalesDW"
-- Used ONLY as spike/demo fixture data for Autovista Discovery-phase
-- development. This is hand-authored representative T-SQL, not an
-- export from a real customer system.
--
-- Scale: 21 tables (15 dbo + 6 staging), 3 views, 11 stored procs,
-- 1 trigger -- matches the "small pilot" target (tens of tables,
-- dozens of packages) chosen for this build.
-- =====================================================================

CREATE DATABASE SalesDW;
GO
USE SalesDW;
GO
CREATE SCHEMA staging;
GO

-- ---------------------------------------------------------------------
-- dbo schema: 15 core tables
-- ---------------------------------------------------------------------
CREATE TABLE dbo.Customers (
    CustomerId      INT IDENTITY(1,1) PRIMARY KEY,
    CustomerName    NVARCHAR(200) NOT NULL,
    Email           NVARCHAR(320) NULL,
    Region          NVARCHAR(100) NULL,
    ModifiedDate    DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
);

CREATE TABLE dbo.Territories (
    TerritoryId     INT IDENTITY(1,1) PRIMARY KEY,
    TerritoryName   NVARCHAR(100) NOT NULL,
    CountryCode     CHAR(2) NOT NULL
);

CREATE TABLE dbo.Addresses (
    AddressId       INT IDENTITY(1,1) PRIMARY KEY,
    Line1           NVARCHAR(200) NOT NULL,
    City            NVARCHAR(100) NOT NULL,
    TerritoryId     INT NOT NULL REFERENCES dbo.Territories(TerritoryId)
);

CREATE TABLE dbo.CustomerAddress (
    CustomerId      INT NOT NULL REFERENCES dbo.Customers(CustomerId),
    AddressId       INT NOT NULL REFERENCES dbo.Addresses(AddressId),
    PRIMARY KEY (CustomerId, AddressId)
);

CREATE TABLE dbo.CreditCards (
    CreditCardId    INT IDENTITY(1,1) PRIMARY KEY,
    CustomerId      INT NOT NULL REFERENCES dbo.Customers(CustomerId),
    CardTypeMasked  NVARCHAR(20) NOT NULL
);

CREATE TABLE dbo.ProductCategory (
    ProductCategoryId INT IDENTITY(1,1) PRIMARY KEY,
    CategoryName    NVARCHAR(100) NOT NULL
);

CREATE TABLE dbo.Products (
    ProductId       INT IDENTITY(1,1) PRIMARY KEY,
    ProductName     NVARCHAR(200) NOT NULL,
    ProductCategoryId INT NOT NULL REFERENCES dbo.ProductCategory(ProductCategoryId),
    ListPrice       DECIMAL(10,2) NOT NULL,
    IsActive        BIT NOT NULL DEFAULT 1
);

CREATE TABLE dbo.Suppliers (
    SupplierId      INT IDENTITY(1,1) PRIMARY KEY,
    SupplierName    NVARCHAR(200) NOT NULL,
    ContactEmail    NVARCHAR(320) NULL
);

CREATE TABLE dbo.Inventory (
    ProductId       INT NOT NULL REFERENCES dbo.Products(ProductId),
    SupplierId      INT NOT NULL REFERENCES dbo.Suppliers(SupplierId),
    QuantityOnHand  INT NOT NULL DEFAULT 0,
    LastCountedDate DATETIME2 NULL,
    PRIMARY KEY (ProductId, SupplierId)
);

CREATE TABLE dbo.ShipMethods (
    ShipMethodId    INT IDENTITY(1,1) PRIMARY KEY,
    MethodName      NVARCHAR(100) NOT NULL
);

CREATE TABLE dbo.Employees (
    EmployeeId      INT IDENTITY(1,1) PRIMARY KEY,
    FullName        NVARCHAR(200) NOT NULL,
    TerritoryId     INT NULL REFERENCES dbo.Territories(TerritoryId)
);

CREATE TABLE dbo.SalesReasons (
    SalesReasonId   INT IDENTITY(1,1) PRIMARY KEY,
    ReasonName      NVARCHAR(100) NOT NULL
);

CREATE TABLE dbo.Orders (
    OrderId         INT IDENTITY(1,1) PRIMARY KEY,
    CustomerId      INT NOT NULL REFERENCES dbo.Customers(CustomerId),
    EmployeeId      INT NULL REFERENCES dbo.Employees(EmployeeId),
    ShipMethodId    INT NOT NULL REFERENCES dbo.ShipMethods(ShipMethodId),
    SalesReasonId   INT NULL REFERENCES dbo.SalesReasons(SalesReasonId),
    OrderDate       DATETIME2 NOT NULL,
    TotalDue        DECIMAL(12,2) NOT NULL,
    ModifiedDate    DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
);

CREATE TABLE dbo.OrderDetails (
    OrderDetailId   INT IDENTITY(1,1) PRIMARY KEY,
    OrderId         INT NOT NULL REFERENCES dbo.Orders(OrderId),
    ProductId       INT NOT NULL REFERENCES dbo.Products(ProductId),
    Quantity        INT NOT NULL,
    UnitPrice       DECIMAL(10,2) NOT NULL
);

CREATE TABLE dbo.ArchiveOrders (
    OrderId         INT NOT NULL PRIMARY KEY,
    CustomerId      INT NOT NULL,
    OrderDate       DATETIME2 NOT NULL,
    TotalDue        DECIMAL(12,2) NOT NULL,
    ArchivedDate    DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
);
GO

CREATE TRIGGER dbo.trg_Orders_UpdateModifiedDate
ON dbo.Orders
AFTER UPDATE
AS
BEGIN
    SET NOCOUNT ON;
    UPDATE o
    SET ModifiedDate = SYSUTCDATETIME()
    FROM dbo.Orders o
    INNER JOIN inserted i ON i.OrderId = o.OrderId;
END;
GO

-- ---------------------------------------------------------------------
-- staging schema: 6 landing tables for SSIS loads
-- ---------------------------------------------------------------------
CREATE TABLE staging.stg_Customers (
    CustomerId      INT NULL,
    CustomerName    NVARCHAR(200) NULL,
    Email           NVARCHAR(320) NULL,
    Region          NVARCHAR(100) NULL
);

CREATE TABLE staging.stg_Orders (
    OrderId         INT NULL,
    CustomerId      INT NULL,
    OrderDate       DATETIME2 NULL,
    TotalDue        DECIMAL(12,2) NULL
);

CREATE TABLE staging.stg_OrderDetails (
    OrderId         INT NULL,
    ProductId       INT NULL,
    Quantity        INT NULL,
    UnitPrice       DECIMAL(10,2) NULL
);

CREATE TABLE staging.stg_Products (
    ProductId       INT NULL,
    ProductName     NVARCHAR(200) NULL,
    ListPrice       DECIMAL(10,2) NULL
);

CREATE TABLE staging.stg_Inventory (
    ProductId       INT NULL,
    SupplierId      INT NULL,
    QuantityOnHand  INT NULL
);

CREATE TABLE staging.stg_Suppliers (
    SupplierId      INT NULL,
    SupplierName    NVARCHAR(200) NULL,
    ContactEmail    NVARCHAR(320) NULL
);
GO

-- ---------------------------------------------------------------------
-- Views (3)
-- ---------------------------------------------------------------------
CREATE VIEW dbo.vw_CustomerOrderSummary AS
SELECT c.CustomerId, c.CustomerName, COUNT(o.OrderId) AS OrderCount, SUM(o.TotalDue) AS LifetimeValue
FROM dbo.Customers c
LEFT JOIN dbo.Orders o ON o.CustomerId = c.CustomerId
GROUP BY c.CustomerId, c.CustomerName;
GO

CREATE VIEW dbo.vw_ActiveProducts AS
SELECT p.ProductId, p.ProductName, pc.CategoryName, p.ListPrice
FROM dbo.Products p
INNER JOIN dbo.ProductCategory pc ON pc.ProductCategoryId = p.ProductCategoryId
WHERE p.IsActive = 1;
GO

CREATE VIEW dbo.vw_InventoryStatus AS
SELECT i.ProductId, p.ProductName, s.SupplierName, i.QuantityOnHand
FROM dbo.Inventory i
INNER JOIN dbo.Products p ON p.ProductId = i.ProductId
INNER JOIN dbo.Suppliers s ON s.SupplierId = i.SupplierId;
GO

-- ---------------------------------------------------------------------
-- Stored procedures (11) -- deliberately varied lineage complexity
-- ---------------------------------------------------------------------
CREATE PROCEDURE dbo.usp_LoadCustomersFromStaging
AS
BEGIN
    SET NOCOUNT ON;
    MERGE dbo.Customers AS tgt
    USING staging.stg_Customers AS src
        ON tgt.CustomerId = src.CustomerId
    WHEN MATCHED THEN
        UPDATE SET tgt.CustomerName = src.CustomerName, tgt.Email = src.Email, tgt.Region = src.Region
    WHEN NOT MATCHED THEN
        INSERT (CustomerName, Email, Region) VALUES (src.CustomerName, src.Email, src.Region);
END;
GO

CREATE PROCEDURE dbo.usp_LoadOrdersFromStaging
AS
BEGIN
    SET NOCOUNT ON;
    INSERT INTO dbo.Orders (CustomerId, ShipMethodId, OrderDate, TotalDue)
    SELECT so.CustomerId, 1, so.OrderDate, so.TotalDue
    FROM staging.stg_Orders so
    WHERE NOT EXISTS (SELECT 1 FROM dbo.Orders o WHERE o.OrderId = so.OrderId);

    INSERT INTO dbo.OrderDetails (OrderId, ProductId, Quantity, UnitPrice)
    SELECT sod.OrderId, sod.ProductId, sod.Quantity, sod.UnitPrice
    FROM staging.stg_OrderDetails sod;
END;
GO

CREATE PROCEDURE dbo.usp_UpdateInventoryFromStaging
AS
BEGIN
    SET NOCOUNT ON;
    UPDATE i
    SET i.QuantityOnHand = si.QuantityOnHand, i.LastCountedDate = SYSUTCDATETIME()
    FROM dbo.Inventory i
    INNER JOIN staging.stg_Inventory si ON si.ProductId = i.ProductId AND si.SupplierId = i.SupplierId;
END;
GO

CREATE PROCEDURE dbo.usp_ArchiveOldOrders
    @CutoffDate DATETIME2
AS
BEGIN
    SET NOCOUNT ON;
    INSERT INTO dbo.ArchiveOrders (OrderId, CustomerId, OrderDate, TotalDue)
    SELECT OrderId, CustomerId, OrderDate, TotalDue
    FROM dbo.Orders
    WHERE OrderDate < @CutoffDate;

    DELETE FROM dbo.OrderDetails WHERE OrderId IN (
        SELECT OrderId FROM dbo.Orders WHERE OrderDate < @CutoffDate
    );

    DELETE FROM dbo.Orders WHERE OrderDate < @CutoffDate;
END;
GO

CREATE PROCEDURE dbo.usp_CalculateSalesReasonStats
AS
BEGIN
    SET NOCOUNT ON;
    SELECT sr.ReasonName, COUNT(*) AS OrderCount, SUM(o.TotalDue) AS TotalRevenue
    FROM dbo.Orders o
    INNER JOIN dbo.SalesReasons sr ON sr.SalesReasonId = o.SalesReasonId
    GROUP BY sr.ReasonName;
END;
GO

CREATE PROCEDURE dbo.usp_MergeProducts
AS
BEGIN
    SET NOCOUNT ON;
    MERGE dbo.Products AS tgt
    USING staging.stg_Products AS src
        ON tgt.ProductId = src.ProductId
    WHEN MATCHED THEN
        UPDATE SET tgt.ProductName = src.ProductName, tgt.ListPrice = src.ListPrice
    WHEN NOT MATCHED THEN
        INSERT (ProductName, ProductCategoryId, ListPrice) VALUES (src.ProductName, 1, src.ListPrice);
END;
GO

CREATE PROCEDURE dbo.usp_SyncSuppliersFromStaging
AS
BEGIN
    SET NOCOUNT ON;
    MERGE dbo.Suppliers AS tgt
    USING staging.stg_Suppliers AS src
        ON tgt.SupplierId = src.SupplierId
    WHEN MATCHED THEN
        UPDATE SET tgt.SupplierName = src.SupplierName, tgt.ContactEmail = src.ContactEmail
    WHEN NOT MATCHED THEN
        INSERT (SupplierName, ContactEmail) VALUES (src.SupplierName, src.ContactEmail);
END;
GO

CREATE PROCEDURE dbo.usp_ValidateOrderTotals
AS
BEGIN
    SET NOCOUNT ON;
    SELECT o.OrderId, o.TotalDue, SUM(od.Quantity * od.UnitPrice) AS ComputedTotal
    FROM dbo.Orders o
    INNER JOIN dbo.OrderDetails od ON od.OrderId = o.OrderId
    GROUP BY o.OrderId, o.TotalDue
    HAVING o.TotalDue <> SUM(od.Quantity * od.UnitPrice);
END;
GO

CREATE PROCEDURE dbo.usp_PurgeStagingTables
AS
BEGIN
    SET NOCOUNT ON;
    TRUNCATE TABLE staging.stg_Customers;
    TRUNCATE TABLE staging.stg_Orders;
    TRUNCATE TABLE staging.stg_OrderDetails;
    TRUNCATE TABLE staging.stg_Products;
    TRUNCATE TABLE staging.stg_Inventory;
    TRUNCATE TABLE staging.stg_Suppliers;
END;
GO

CREATE PROCEDURE dbo.usp_RebuildIndexes
AS
BEGIN
    SET NOCOUNT ON;
    ALTER INDEX ALL ON dbo.Orders REBUILD;
    ALTER INDEX ALL ON dbo.OrderDetails REBUILD;
END;
GO

-- Deliberately hard to statically parse: table name assembled at runtime
-- and executed via sp_executesql. sqlglot cannot resolve @TableName, so
-- this is the representative "dynamic SQL" case routed to the LLM
-- fallback / unresolved path.
CREATE PROCEDURE dbo.usp_DynamicReportBuilder
    @TableName SYSNAME,
    @RegionFilter NVARCHAR(100)
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @sql NVARCHAR(MAX);
    SET @sql = N'SELECT * FROM ' + QUOTENAME(@TableName) + N' WHERE Region = @Region';
    EXEC sp_executesql @sql, N'@Region NVARCHAR(100)', @Region = @RegionFilter;
END;
GO
